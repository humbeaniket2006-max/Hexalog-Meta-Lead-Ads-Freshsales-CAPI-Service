import asyncio
import random

from app.config import Settings
from app.logging_config import get_logger
from app.security import pii_ref
from app.services.freshsales_client import FreshsalesClient

logger = get_logger(__name__)


def extract_field_data(meta_lead: dict[str, object]) -> tuple[str | None, str | None]:
    email: str | None = None
    phone: str | None = None
    fields = meta_lead.get("field_data", [])
    if not isinstance(fields, list):
        return None, None
    for item in fields:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        values = item.get("values")
        first = values[0] if isinstance(values, list) and values else None
        if not isinstance(first, str):
            continue
        if name == "email":
            email = first
        if name in {"phone_number", "phone"}:
            phone = first
    return email, phone


async def match_with_backoff(
    freshsales: FreshsalesClient,
    settings: Settings,
    email: str | None,
    phone: str | None,
    correlation_id: str,
) -> dict[str, object] | None:
    for attempt in range(1, settings.match_retry_attempts + 1):
        lead = await freshsales.find_lead_by_contact(email=email, phone=phone)
        if lead is not None:
            logger.info("lead_matched", correlation_id=correlation_id, attempt=attempt)
            return lead
        logger.info(
            "lead_match_miss",
            correlation_id=correlation_id,
            attempt=attempt,
            email_ref=pii_ref(email),
            phone_ref=pii_ref(phone),
        )
        if attempt < settings.match_retry_attempts:
            await asyncio.sleep(random.uniform(settings.match_retry_min_seconds, settings.match_retry_max_seconds))
    return None
