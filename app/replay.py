import asyncio
from datetime import datetime, timezone
from uuid import uuid4

from app.config import Settings
from app.db.repository import Repository
from app.logging_config import get_logger
from app.webhooks.meta_lead_capture import process_meta_lead

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
