from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.repository import Repository, freshsales_dedupe_key
from app.db.session import get_session
from app.logging_config import get_logger
from app.models.webhooks import FreshsalesWebhookPayload
from app.security import verify_shared_secret
from app.services.alerts import AlertService
from app.services.freshsales_client import FreshsalesClient
from app.services.meta_capi import MetaCapiClient

router = APIRouter(prefix="/webhooks/freshsales", tags=["freshsales"])
logger = get_logger(__name__)


def contact_identity_from_envelope(envelope: dict[str, object]) -> tuple[str | None, str | None]:
    email = _direct_string_value(envelope, {"email"})
    phone = _direct_string_value(envelope, {"mobile_number", "mobile", "phone_number", "phone", "work_number"})
    contact_values = [value for key, value in envelope.items() if "contact" in key.lower()]
    if email is None:
        email = _first_string_value(contact_values, {"email"})
    if phone is None:
        phone = _first_string_value(contact_values, {"mobile_number", "mobile", "phone_number", "phone", "work_number"})
    return email, phone


def _direct_string_value(record: dict[str, object], keys: set[str]) -> str | None:
    for key, value in record.items():
        if key in keys and isinstance(value, str) and value.strip():
            return value
    return None


def _first_string_value(value: object, keys: set[str]) -> str | None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in keys and isinstance(item, str) and item.strip():
                return item
        for item in value.values():
            found = _first_string_value(item, keys)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _first_string_value(item, keys)
            if found:
                return found
    return None


@router.post("/status")
async def receive_freshsales_status(
    request: Request,
    x_freshsales_signature: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    await request.app.state.rate_limiter.check(request)
    if not verify_shared_secret(x_freshsales_signature, settings.freshsales_webhook_secret):
        logger.warning("freshsales_auth_rejected")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="authentication failed")
    try:
        payload = FreshsalesWebhookPayload.model_validate(await request.json())
    except (ValidationError, ValueError):
        logger.warning("freshsales_payload_invalid")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid payload") from None

    correlation_id = str(uuid4())
    repo = Repository(session)
    window = payload.occurred_at.strftime("%Y%m%d%H%M")
    dedupe_key = freshsales_dedupe_key(payload.record_id, payload.status.value, window)
    if not await repo.acquire_dedupe(dedupe_key):
        logger.info("freshsales_duplicate_skipped", correlation_id=correlation_id, record_id=payload.record_id)
        return {"status": "ok"}
    if not await repo.has_record(payload.record_id, payload.record_type.value):
        logger.warning("freshsales_record_not_known", correlation_id=correlation_id, record_id=payload.record_id)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="record not recognized")

    http_client = request.app.state.http_client
    freshsales = FreshsalesClient(settings, http_client)
    meta_capi = MetaCapiClient(settings, http_client)
    alerts = AlertService(settings, http_client)
    try:
        logger.info("freshsales_status_received", correlation_id=correlation_id, record_id=payload.record_id, status=payload.status.value)
        record = await freshsales.get_record(payload.record_type.value, payload.record_id)
        envelope = record.get(payload.record_type.value, record)
        if not isinstance(envelope, dict):
            raise RuntimeError("Freshsales record malformed")
        custom_fields = envelope.get("custom_field", {})
        if not isinstance(custom_fields, dict):
            raise RuntimeError("Freshsales custom_field malformed")
        lead_id = custom_fields.get(settings.freshsales_meta_lead_field)
        if not isinstance(lead_id, str) or not lead_id:
            raise RuntimeError("Freshsales record missing meta lead id")
        event_name = settings.event_mapping[payload.status.value]
        value = float(envelope.get("amount", 0.0)) if payload.status.value == "Won" else None
        currency = str(envelope.get("currency", "USD")) if payload.status.value == "Won" else None
        email, phone = contact_identity_from_envelope(envelope)
        response = await meta_capi.send_crm_event(
            event_name,
            lead_id,
            dedupe_key,
            value,
            currency,
            email=email,
            phone=phone,
        )
        await repo.mark_dedupe_success(dedupe_key)
        logger.info(
            "meta_capi_confirmed",
            correlation_id=correlation_id,
            event_name=event_name,
            lead_id=lead_id,
            events_received=response.get("events_received") if isinstance(response, dict) else None,
        )
        return {"status": "ok"}
    except Exception as exc:
        await repo.dead_letter(correlation_id, "freshsales_status", payload.model_dump(), type(exc).__name__, settings.max_http_attempts)
        await repo.mark_dedupe_failed(dedupe_key)
        await alerts.fire("Freshsales status CAPI send failed", {"correlation_id": correlation_id, "error": type(exc).__name__})
        logger.error("freshsales_flow_failed", correlation_id=correlation_id, error=type(exc).__name__)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="processing failed") from None
