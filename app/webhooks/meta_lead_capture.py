from uuid import uuid4

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response, status
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.repository import Repository, meta_dedupe_key
from app.db.session import get_session
from app.logging_config import get_logger
from app.matching import match_with_backoff
from app.models.webhooks import MetaLeadgenWebhookPayload
from app.security import verify_meta_signature
from app.services.alerts import AlertService
from app.services.freshsales_client import DuplicateContactError, FreshsalesClient
from app.models.webhooks import MetaGraphLeadPayload
from app.services.meta_graph import MetaGraphClient

router = APIRouter(prefix="/webhooks/meta", tags=["meta"])
logger = get_logger(__name__)


class FreshsalesContactCreateError(RuntimeError):
    pass


def extract_meta_fields(meta_lead: MetaGraphLeadPayload) -> dict[str, str]:
    fields: dict[str, str] = {}
    for item in meta_lead.field_data:
        first = item.values[0].strip() if item.values else ""
        if first:
            fields[item.name] = first
    return fields


def split_full_name(full_name: str) -> tuple[str, str | None]:
    parts = full_name.strip().split()
    if not parts:
        return "", None
    if len(parts) == 1:
        return parts[0], None
    return parts[0], " ".join(parts[1:])


def build_freshsales_contact_fields(meta_fields: dict[str, str], settings: Settings) -> dict[str, object]:
    contact_fields: dict[str, object] = {}
    custom_field: dict[str, object] = {}
    for meta_name, value in meta_fields.items():
        freshsales_name = settings.meta_to_freshsales_field_mapping.get(meta_name)
        if not freshsales_name:
            continue
        if meta_name == "full_name":
            first_name, last_name = split_full_name(value)
            if first_name:
                contact_fields[freshsales_name] = first_name
            if last_name:
                contact_fields.setdefault("last_name", last_name)
            continue
        if freshsales_name.startswith("custom_field."):
            custom_field[freshsales_name.removeprefix("custom_field.")] = value
        else:
            contact_fields[freshsales_name] = value
    if custom_field:
        contact_fields["custom_field"] = custom_field
    return contact_fields


def existing_value(record: dict[str, object], field_name: str) -> object:
    if field_name.startswith("custom_field."):
        custom_field = record.get("custom_field")
        if isinstance(custom_field, dict):
            return custom_field.get(field_name.removeprefix("custom_field."))
        return None
    return record.get(field_name)


def empty_only_fields(record: dict[str, object], contact_fields: dict[str, object]) -> dict[str, object]:
    updates: dict[str, object] = {}
    custom_updates: dict[str, object] = {}
    for field_name, value in contact_fields.items():
        if field_name == "custom_field" and isinstance(value, dict):
            for custom_name, custom_value in value.items():
                if not existing_value(record, f"custom_field.{custom_name}"):
                    custom_updates[custom_name] = custom_value
            continue
        if not existing_value(record, field_name):
            updates[field_name] = value
    if custom_updates:
        updates["custom_field"] = custom_updates
    return updates


@router.get("/leadgen")
async def verify_meta_webhook(
    hub_mode: str = Query(alias="hub.mode"),
    hub_challenge: str = Query(alias="hub.challenge"),
    hub_verify_token: str = Query(alias="hub.verify_token"),
    settings: Settings = Depends(get_settings),
) -> Response:
    if hub_mode == "subscribe" and hub_verify_token == settings.meta_verify_token.get_secret_value():
        return Response(content=hub_challenge, media_type="text/plain")
    logger.warning("meta_verification_rejected")
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="verification failed")


def build_campaign_fields(meta_lead: MetaGraphLeadPayload, settings: Settings) -> dict[str, str]:
    """Freshsales custom fields for the Meta campaign context; empty values are omitted."""
    candidates = {
        settings.freshsales_campaign_name_field: meta_lead.campaign_name,
        settings.freshsales_campaign_id_field: meta_lead.campaign_id,
        settings.freshsales_ad_set_field: meta_lead.adset_name,
        settings.freshsales_ad_name_field: meta_lead.ad_name,
    }
    return {field: value.strip() for field, value in candidates.items() if value and value.strip()}


async def stamp_contact(
    freshsales: FreshsalesClient,
    record_id: str,
    leadgen_id: str,
    campaign_fields: dict[str, str],
    correlation_id: str,
) -> None:
    """Write the lead id plus campaign fields; never let a bad campaign field block the lead id."""
    try:
        await freshsales.update_meta_lead_id(record_id, leadgen_id, campaign_fields)
    except httpx.HTTPStatusError as exc:
        if not campaign_fields:
            raise
        logger.error(
            "campaign_fields_write_failed",
            correlation_id=correlation_id,
            leadgen_id=leadgen_id,
            status_code=exc.response.status_code,
        )
        await freshsales.update_meta_lead_id(record_id, leadgen_id)


async def create_contact_with_campaign(
    freshsales: FreshsalesClient,
    contact_fields: dict[str, object],
    leadgen_id: str,
    campaign_fields: dict[str, str],
    correlation_id: str,
) -> dict[str, object]:
    if campaign_fields:
        with_campaign = dict(contact_fields)
        with_campaign["custom_field"] = {**(contact_fields.get("custom_field") or {}), **campaign_fields}  # type: ignore[dict-item]
        try:
            return await freshsales.create_contact(with_campaign, leadgen_id)
        except httpx.HTTPStatusError as exc:
            logger.error(
                "campaign_fields_create_failed",
                correlation_id=correlation_id,
                leadgen_id=leadgen_id,
                status_code=exc.response.status_code,
            )
    return await freshsales.create_contact(contact_fields, leadgen_id)


@router.post("/leadgen")
async def receive_meta_leadgen(
    request: Request,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    await request.app.state.rate_limiter.check(request)
    body = await request.body()
    if not verify_meta_signature(body, request.headers.get("X-Hub-Signature-256"), settings.meta_app_secret):
        logger.warning("meta_signature_rejected")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="signature verification failed")
    try:
        payload = MetaLeadgenWebhookPayload.model_validate_json(body)
    except ValidationError:
        logger.warning("meta_payload_invalid")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid payload") from None

    repo = Repository(session)
    for entry in payload.entry:
        for change in entry.changes:
            if change.field != "leadgen":
                continue
            leadgen_id = change.value.leadgen_id
            correlation_id = str(uuid4())
            if not await repo.acquire_dedupe(meta_dedupe_key(leadgen_id)):
                logger.info("meta_duplicate_skipped", correlation_id=correlation_id, leadgen_id=leadgen_id)
                continue
            logger.info("meta_lead_received", correlation_id=correlation_id, leadgen_id=leadgen_id)
            background_tasks.add_task(process_meta_lead, request.app.state, settings, leadgen_id, correlation_id)
    return {"status": "ok"}


async def process_meta_lead(app_state: object, settings: Settings, leadgen_id: str, correlation_id: str) -> None:
    """Fetch the lead from Meta and stamp/create its Freshsales contact. The dedupe key is already 'processing'."""
    dedupe_key = meta_dedupe_key(leadgen_id)
    http_client = app_state.http_client  # type: ignore[attr-defined]
    async with app_state.lead_semaphore, app_state.sessionmaker() as session:  # type: ignore[attr-defined]
        repo = Repository(session)
        meta_graph = MetaGraphClient(settings, http_client)
        freshsales = FreshsalesClient(settings, http_client)
        alerts = AlertService(settings, http_client)
        try:
            meta_lead = await meta_graph.fetch_lead(leadgen_id)
            meta_fields = extract_meta_fields(meta_lead)
            campaign_fields = build_campaign_fields(meta_lead, settings)
            if not meta_lead.campaign_id:
                logger.warning("campaign_fields_missing", correlation_id=correlation_id, leadgen_id=leadgen_id)
            email = meta_fields.get("email")
            phone = meta_fields.get("phone_number") or meta_fields.get("phone")
            if not email and not phone:
                raise FreshsalesContactCreateError("Meta lead has no email or phone to match on")
            matched = await match_with_backoff(freshsales, settings, email, phone, correlation_id)
            if matched is not None:
                record_id = str(matched.get("id", ""))
                if not record_id:
                    raise RuntimeError("Freshsales match missing id")
                await stamp_contact(freshsales, record_id, leadgen_id, campaign_fields, correlation_id)
                logger.info("freshsales_matched_contact_updated", correlation_id=correlation_id, leadgen_id=leadgen_id, record_id=record_id)
            else:
                logger.warning("freshsales_contact_not_found_after_retries", correlation_id=correlation_id, leadgen_id=leadgen_id)
                contact_fields = build_freshsales_contact_fields(meta_fields, settings)
                try:
                    created = await create_contact_with_campaign(freshsales, contact_fields, leadgen_id, campaign_fields, correlation_id)
                except DuplicateContactError as exc:
                    try:
                        await stamp_contact(freshsales, exc.object_id, leadgen_id, campaign_fields, correlation_id)
                    except Exception as update_exc:
                        raise FreshsalesContactCreateError("Duplicate contact found but meta lead id update failed") from update_exc
                    record_id = exc.object_id
                    logger.info("freshsales_duplicate_mobile_resolved", correlation_id=correlation_id, leadgen_id=leadgen_id, record_id=record_id)
                except Exception as exc:
                    if isinstance(exc, httpx.HTTPStatusError):
                        logger.error(
                            "freshsales_contact_create_failed",
                            correlation_id=correlation_id,
                            leadgen_id=leadgen_id,
                            status_code=exc.response.status_code,
                        )
                    raise FreshsalesContactCreateError("Freshsales contact create failed") from exc
                else:
                    record_id = str(created.get("id", ""))
                    if not record_id:
                        raise FreshsalesContactCreateError("Freshsales created contact missing id")
                    logger.info("freshsales_contact_created", correlation_id=correlation_id, record_id=record_id)
            await repo.save_lead_state(leadgen_id, record_id, record_type="contact")
            await repo.mark_dedupe_success(dedupe_key)
            logger.info(
                "meta_lead_processed",
                correlation_id=correlation_id,
                leadgen_id=leadgen_id,
                campaign_id=meta_lead.campaign_id,
                record_id=record_id,
            )
        except Exception as exc:
            await session.rollback()
            await repo.dead_letter(correlation_id, "meta_lead_capture", {"leadgen_id": leadgen_id}, type(exc).__name__, settings.max_http_attempts)
            await repo.mark_dedupe_failed(dedupe_key)
            await alerts.fire("Meta lead capture failed", {"correlation_id": correlation_id, "error": type(exc).__name__})
            logger.error("meta_flow_failed", correlation_id=correlation_id, leadgen_id=leadgen_id, error=type(exc).__name__)
