import time
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.repository import Repository
from app.db.session import get_session
from app.logging_config import get_logger
from app.security import verify_shared_secret
from app.services.alerts import AlertService
from app.services.freshsales_client import FreshsalesClient
from app.services.meta_capi_client import MetaCapiClient

router = APIRouter(prefix="/webhooks/freshsales", tags=["freshsales"])
logger = get_logger(__name__)


class FreshsalesLeadEventPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_name: Literal["Qualified", "NotQualified", "Conversion", "DealCreated"]
    contact_id: str | None = Field(default=None, min_length=1, max_length=80)
    deal_id: str | None = Field(default=None, min_length=1, max_length=80)
    meta_lead_id: str | None = Field(default=None, min_length=1, max_length=80)
    amount: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def require_event_fields(self) -> "FreshsalesLeadEventPayload":
        if self.event_name in {"Qualified", "NotQualified"} and not self.contact_id:
            raise ValueError("contact_id is required for lead qualification events")
        if self.event_name == "Conversion":
            missing = [field for field in ("deal_id", "meta_lead_id", "amount") if getattr(self, field) is None]
            if missing:
                raise ValueError("deal_id, meta_lead_id, and amount are required for Conversion events")
        if self.event_name == "DealCreated" and not self.deal_id:
            raise ValueError("deal_id is required for DealCreated events")
        return self


def first_linked_contact_id(deal_response: dict[str, object]) -> str | None:
    containers = [deal_response]
    deal = deal_response.get("deal")
    if isinstance(deal, dict):
        containers.insert(0, deal)

    for container in containers:
        for key in ("contacts", "deal_contacts"):
            contact_id = _contact_id_from_contacts(container.get(key))
            if contact_id:
                return contact_id
        contact_id = _string_or_int_id(container.get("contact_id"))
        if contact_id:
            return contact_id
        contact_id = _first_id_from_list(container.get("contact_ids"))
        if contact_id:
            return contact_id
    return None


def _contact_id_from_contacts(value: object) -> str | None:
    if isinstance(value, list):
        for item in value:
            contact_id = _contact_id_from_contact(item)
            if contact_id:
                return contact_id
    if isinstance(value, dict):
        records = value.get("records")
        if isinstance(records, list):
            return _contact_id_from_contacts(records)
        return _contact_id_from_contact(value)
    return None


def _contact_id_from_contact(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    contact_id = _string_or_int_id(value.get("id"))
    if contact_id:
        return contact_id
    nested = value.get("contact")
    if isinstance(nested, dict):
        return _string_or_int_id(nested.get("id"))
    return None


def _first_id_from_list(value: object) -> str | None:
    if not isinstance(value, list):
        return None
    for item in value:
        contact_id = _string_or_int_id(item)
        if contact_id:
            return contact_id
    return None


def _string_or_int_id(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, int):
        return str(value)
    return None


@router.post("/lead-event")
async def receive_freshsales_lead_event(
    request: Request,
    x_internal_secret: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    await request.app.state.rate_limiter.check(request)
    if not verify_shared_secret(x_internal_secret, settings.freshsales_webhook_secret):
        logger.warning("freshsales_lead_event_auth_rejected")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="authentication failed")

    try:
        payload = FreshsalesLeadEventPayload.model_validate(await request.json())
    except (ValidationError, ValueError):
        logger.warning("freshsales_lead_event_payload_invalid")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid payload") from None

    correlation_id = str(uuid4())
    repo = Repository(session)
    http_client = request.app.state.http_client
    alerts = AlertService(settings, http_client)

    if payload.event_name == "DealCreated":
        freshsales = FreshsalesClient(settings, http_client)
        try:
            deal = await freshsales.get_deal(str(payload.deal_id))
            contact_id = first_linked_contact_id(deal)
        except Exception as exc:
            await repo.dead_letter(
                correlation_id,
                "freshsales_lead_event",
                payload.model_dump(),
                type(exc).__name__,
                settings.max_http_attempts,
            )
            await alerts.fire("Freshsales lead event deal stamp failed", {"correlation_id": correlation_id, "error": type(exc).__name__})
            logger.error("freshsales_lead_event_flow_failed", correlation_id=correlation_id, error=type(exc).__name__)
            return {"status": "ok"}

        if not contact_id:
            logger.warning(
                "freshsales_deal_no_linked_contact",
                correlation_id=correlation_id,
                deal_id=payload.deal_id,
            )
            return {"status": "ok"}

        leadgen_id = await repo.get_leadgen_id_by_record_id(contact_id)
        if not leadgen_id:
            logger.warning(
                "freshsales_lead_event_no_match",
                correlation_id=correlation_id,
                event_name=payload.event_name,
                contact_id=contact_id,
            )
            return {"status": "ok"}

        try:
            await freshsales.update_field(
                record_id=str(payload.deal_id),
                field_name="cf_meta_lead_id",
                value=leadgen_id,
                module="deals",
            )
        except Exception as exc:
            await repo.dead_letter(
                correlation_id,
                "freshsales_lead_event",
                payload.model_dump(),
                type(exc).__name__,
                settings.max_http_attempts,
            )
            await alerts.fire("Freshsales lead event deal stamp failed", {"correlation_id": correlation_id, "error": type(exc).__name__})
            logger.error("freshsales_lead_event_flow_failed", correlation_id=correlation_id, error=type(exc).__name__)
            return {"status": "ok"}

        logger.info(
            "freshsales_deal_meta_lead_id_stamped",
            correlation_id=correlation_id,
            deal_id=payload.deal_id,
            leadgen_id=leadgen_id,
        )
        return {"status": "ok"}

    leadgen_id: str | None
    if payload.event_name == "Conversion":
        leadgen_id = payload.meta_lead_id
    else:
        leadgen_id = await repo.get_leadgen_id_by_record_id(str(payload.contact_id))

    if not leadgen_id:
        logger.warning(
            "freshsales_lead_event_no_match",
            correlation_id=correlation_id,
            event_name=payload.event_name,
            contact_id=payload.contact_id,
        )
        return {"status": "ok"}

    meta_capi = MetaCapiClient(settings, http_client)
    try:
        await meta_capi.send_event(
            leadgen_id,
            payload.event_name,
            event_time=int(time.time()),
            amount=payload.amount if payload.event_name == "Conversion" else None,
        )
    except Exception as exc:
        await repo.dead_letter(
            correlation_id,
            "freshsales_lead_event",
            payload.model_dump(),
            type(exc).__name__,
            settings.max_http_attempts,
        )
        await alerts.fire("Freshsales lead event CAPI send failed", {"correlation_id": correlation_id, "error": type(exc).__name__})
        logger.error("freshsales_lead_event_flow_failed", correlation_id=correlation_id, error=type(exc).__name__)
        return {"status": "ok"}

    logger.info(
        "freshsales_lead_event_sent",
        correlation_id=correlation_id,
        event_name=payload.event_name,
        leadgen_id=leadgen_id,
    )
    return {"status": "ok"}
