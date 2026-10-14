import asyncio
import time
from datetime import datetime, timezone
from uuid import uuid4

from app.config import Settings
from app.db.repository import Repository, meta_dedupe_key
from app.logging_config import get_logger
from app.services.freshsales_client import FreshsalesClient
from app.services.meta_graph import MetaGraphClient
from app.webhooks.meta_lead_capture import build_campaign_fields, extract_meta_fields, process_meta_lead, stamp_contact

logger = get_logger(__name__)

MIN_IDLE_SECONDS = 600
KEY_PREFIX = "meta_lead_capture:"


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def replay_once(app_state: object, settings: Settings) -> int:
    """Retry Meta leads that failed or were abandoned mid-flight. Backs off as a lead gets older."""
    sessionmaker = app_state.sessionmaker  # type: ignore[attr-defined]
    async with sessionmaker() as session:
        candidates = await Repository(session).replayable_meta_leads(MIN_IDLE_SECONDS, settings.lead_replay_max_age_hours)
    now = datetime.now(timezone.utc)
    replayed = 0
    for key, created_at, updated_at in candidates:
        # Wait at least a quarter of the lead's age since its last attempt (10 min minimum).
        wait = max(MIN_IDLE_SECONDS, (now - _aware(created_at)).total_seconds() / 4)
        if (now - _aware(updated_at)).total_seconds() < wait:
            continue
        async with sessionmaker() as session:
            if not await Repository(session).claim_for_replay(key):
                continue
        leadgen_id = key.removeprefix(KEY_PREFIX)
        logger.info("meta_lead_replay", leadgen_id=leadgen_id)
        await process_meta_lead(app_state, settings, leadgen_id, str(uuid4()))
        replayed += 1
    return replayed


async def replay_loop(app_state: object, settings: Settings) -> None:
    while True:
        await asyncio.sleep(settings.lead_replay_interval_seconds)
        try:
            await replay_once(app_state, settings)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("meta_lead_replay_failed", error=type(exc).__name__)


async def reconcile_missing_leads(app_state: object, settings: Settings) -> int:
    """Pull recent leads from Meta and process any the webhook never delivered (e.g. server asleep or down)."""
    sessionmaker = app_state.sessionmaker  # type: ignore[attr-defined]
    graph = MetaGraphClient(settings, app_state.http_client)  # type: ignore[attr-defined]
    since = int(time.time()) - settings.lead_reconcile_lookback_hours * 3600
    page_id, page_token = await graph.resolve_page()
    recovered = 0
    for form_id in await graph.list_form_ids(page_id, page_token):
        for leadgen_id in await graph.list_lead_ids_since(form_id, since):
            async with sessionmaker() as session:
                if not await Repository(session).acquire_dedupe_if_new(meta_dedupe_key(leadgen_id)):
                    continue
            logger.info("meta_lead_reconciled", leadgen_id=leadgen_id, form_id=form_id)
            await process_meta_lead(app_state, settings, leadgen_id, str(uuid4()))
            recovered += 1
    logger.info("meta_lead_reconcile_done", recovered=recovered, lookback_hours=settings.lead_reconcile_lookback_hours)
    return recovered


async def reconcile_loop(app_state: object, settings: Settings) -> None:
    """Run once shortly after startup (a wake-up from sleep), then periodically."""
    await asyncio.sleep(settings.lead_reconcile_startup_delay_seconds)
    while True:
        try:
            await reconcile_missing_leads(app_state, settings)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("meta_lead_reconcile_failed", error=type(exc).__name__)
        await asyncio.sleep(settings.lead_reconcile_interval_seconds)


def _contact_matches_lead(contact: dict[str, object], email: str | None, phone: str | None) -> bool:
    """Exact email or last-10-digit phone match, checked on the full contact (search results can be fuzzy)."""
    emails = {str(contact.get("email") or "").strip().lower()}
    for item in contact.get("emails") or []:  # type: ignore[union-attr]
        if isinstance(item, dict):
            emails.add(str(item.get("value") or "").strip().lower())
    emails.discard("")
    digits = lambda value: "".join(ch for ch in str(value or "") if ch.isdigit())[-10:]  # noqa: E731
    phones = {digits(contact.get(key)) for key in ("mobile_number", "work_number")}
    phones.discard("")
    return bool(email and email.strip().lower() in emails) or bool(phone and digits(phone) in phones)


async def link_duplicate_contacts(app_state: object, settings: Settings, dry_run: bool = False) -> int:
    """Stamp other contacts for the same person (e.g. created later by Freshsales' own Meta sync) with the lead id.

    Sales often work the duplicate that carries the owner, so it needs the lead id too. Never overwrites an
    existing lead id.
    """
    sessionmaker = app_state.sessionmaker  # type: ignore[attr-defined]
    http_client = app_state.http_client  # type: ignore[attr-defined]
    graph = MetaGraphClient(settings, http_client)
    freshsales = FreshsalesClient(settings, http_client)
    async with sessionmaker() as session:
        pairs = await Repository(session).recent_lead_states(settings.lead_link_lookback_hours)
    linked = 0
    for leadgen_id, record_id in pairs:
        correlation_id = str(uuid4())
        try:
            lead = await graph.fetch_lead(leadgen_id)
            fields = extract_meta_fields(lead)
            email = fields.get("email")
            phone = fields.get("phone_number") or fields.get("phone")
            campaign_fields = build_campaign_fields(lead, settings)
            candidate_ids: dict[str, None] = {}
            if email:
                for match in await freshsales.search_contacts_by_field("email", email):
                    candidate_ids[str(match.get("id", ""))] = None
            if phone:
                for match in await freshsales.search_contacts_by_field("mobile_number", phone):
                    candidate_ids[str(match.get("id", ""))] = None
            candidate_ids.pop("", None)
            candidate_ids.pop(record_id, None)
            for candidate_id in candidate_ids:
                record = await freshsales.get_record("contact", candidate_id)
                contact = record.get("contact", record)
                if not isinstance(contact, dict) or not _contact_matches_lead(contact, email, phone):
                    continue
                custom_field = contact.get("custom_field")
                if isinstance(custom_field, dict) and custom_field.get(settings.freshsales_meta_lead_field):
                    continue
                if not dry_run:
                    await stamp_contact(freshsales, candidate_id, leadgen_id, campaign_fields, correlation_id)
                logger.info(
                    "meta_lead_would_link_duplicate_contact" if dry_run else "meta_lead_linked_duplicate_contact",
                    correlation_id=correlation_id,
                    leadgen_id=leadgen_id,
                    record_id=candidate_id,
                    stamped_record_id=record_id,
                )
                linked += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("meta_lead_link_failed", leadgen_id=leadgen_id, error=type(exc).__name__)
    logger.info("meta_lead_link_done", linked=linked, leads_checked=len(pairs))
    return linked


async def link_loop(app_state: object, settings: Settings) -> None:
    await asyncio.sleep(60)
    while True:
        try:
            await link_duplicate_contacts(app_state, settings)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("meta_lead_link_loop_failed", error=type(exc).__name__)
        await asyncio.sleep(settings.lead_link_interval_seconds)
