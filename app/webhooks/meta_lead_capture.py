from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
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
from app.services.freshsales_client import FreshsalesClient
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


@router.post("/leadgen")
async def receive_meta_leadgen(
    request: Request,
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
    http_client = request.app.state.http_client
    meta_graph = MetaGraphClient(settings, http_client)
    freshsales = FreshsalesClient(settings, http_client)
    alerts = AlertService(settings, http_client)

    for entry in payload.entry:
        for change in entry.changes:
            if change.field != "leadgen":
                continue
            leadgen_id = change.value.leadgen_id
            correlation_id = str(uuid4())
            dedupe_key = meta_dedupe_key(leadgen_id)
            if not await repo.acquire_dedupe(dedupe_key):
                logger.info("meta_duplicate_skipped", correlation_id=correlation_id, leadgen_id=leadgen_id)
                continue
            try:
                logger.info("meta_lead_received", correlation_id=correlation_id, leadgen_id=leadgen_id)
                meta_lead = await meta_graph.fetch_lead(leadgen_id)
                meta_fields = extract_meta_fields(meta_lead)
                email = meta_fields.get("email")
                phone = meta_fields.get("phone_number") or meta_fields.get("phone")
                if not email and not phone:
                    raise FreshsalesContactCreateError("Meta lead has no email or phone to match on")
                matched = await match_with_backoff(freshsales, settings, email, phone, correlation_id)
                if matched is not None:
                    record_id = str(matched.get("id", ""))
                    if not record_id:
                        raise RuntimeError("Freshsales match missing id")
                    await freshsales.update_meta_lead_id(record_id, leadgen_id)
                    logger.info(
                        "freshsales_matched_contact_updated",
                        correlation_id=correlation_id,
                        leadgen_id=leadgen_id,
                        mobile_number=phone,
                        record_id=record_id,
                    )
                    await repo.save_lead_state(leadgen_id, record_id, record_type="contact")
                    await repo.mark_dedupe_success(dedupe_key)
                    continue
                logger.warning(
                    "freshsales_contact_not_found_after_retries",
                    correlation_id=correlation_id,
                    leadgen_id=leadgen_id,
                )
                raise FreshsalesContactCreateError(
                    "No matching Freshsales contact found after retries (native Meta integration may not have created it yet)"
                )
            except Exception as exc:
                await repo.dead_letter(correlation_id, "meta_lead_capture", payload.model_dump(), type(exc).__name__, settings.max_http_attempts)
                await repo.mark_dedupe_failed(dedupe_key)
                await alerts.fire("Meta lead capture failed", {"correlation_id": correlation_id, "error": type(exc).__name__})
                logger.error("meta_flow_failed", correlation_id=correlation_id, error=type(exc).__name__)
                raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="processing failed") from None
    return {"status": "ok"}
