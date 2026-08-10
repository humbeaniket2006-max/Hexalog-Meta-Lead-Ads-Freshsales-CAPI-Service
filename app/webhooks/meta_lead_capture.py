from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.repository import Repository, meta_dedupe_key
from app.db.session import get_session
from app.logging_config import get_logger
from app.matching import extract_field_data, match_with_backoff
from app.models.webhooks import MetaLeadgenWebhookPayload
from app.security import verify_meta_signature
from app.services.alerts import AlertService
from app.services.freshsales_client import FreshsalesClient
from app.services.meta_graph import MetaGraphClient

router = APIRouter(prefix="/webhooks/meta", tags=["meta"])
logger = get_logger(__name__)


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
                email, phone = extract_field_data(meta_lead)
                matched = await match_with_backoff(freshsales, settings, email, phone, correlation_id)
                if matched is None:
                    raise RuntimeError("Freshsales record not found after retry")
                record_id = str(matched.get("id", ""))
                if not record_id:
                    raise RuntimeError("Freshsales match missing id")
                await freshsales.update_meta_lead_id(record_id, leadgen_id)
                await repo.save_lead_state(leadgen_id, record_id)
                await repo.mark_dedupe_success(dedupe_key)
                logger.info("freshsales_lead_updated", correlation_id=correlation_id, record_id=record_id)
            except Exception as exc:
                await repo.dead_letter(correlation_id, "meta_lead_capture", payload.model_dump(), type(exc).__name__, settings.max_http_attempts)
                await repo.mark_dedupe_failed(dedupe_key)
                await alerts.fire("Meta lead capture failed", {"correlation_id": correlation_id, "error": type(exc).__name__})
                logger.error("meta_flow_failed", correlation_id=correlation_id, error=type(exc).__name__)
                raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="processing failed") from None
    return {"status": "ok"}
