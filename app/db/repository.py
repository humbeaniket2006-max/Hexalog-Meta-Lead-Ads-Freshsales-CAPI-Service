import json
from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Base, DeadLetter, DedupeEvent, LeadState


class Repository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def init_schema(self) -> None:
        await self.session.run_sync(lambda sync_session: Base.metadata.create_all(sync_session.get_bind()))

    async def health(self) -> bool:
        await self.session.execute(text("SELECT 1"))
        return True

    async def acquire_dedupe(self, key: str) -> bool:
        try:
            await self.session.execute(insert(DedupeEvent).values(dedupe_key=key, status="processing"))
            await self.session.commit()
            return True
        except IntegrityError:
            await self.session.rollback()
            result = await self.session.execute(select(DedupeEvent.status).where(DedupeEvent.dedupe_key == key))
            status = result.scalar_one_or_none()
            if status in {"success", "processing"}:
                return False
            await self.session.execute(update(DedupeEvent).where(DedupeEvent.dedupe_key == key).values(status="processing"))
            await self.session.commit()
            return True

    async def mark_dedupe_success(self, key: str) -> None:
        await self.session.execute(update(DedupeEvent).where(DedupeEvent.dedupe_key == key).values(status="success"))
        await self.session.commit()

    async def mark_dedupe_failed(self, key: str) -> None:
        await self.session.execute(update(DedupeEvent).where(DedupeEvent.dedupe_key == key).values(status="failed"))
        await self.session.commit()

    async def save_lead_state(self, leadgen_id: str, freshsales_record_id: str, record_type: str = "contact") -> None:
        stmt = insert(LeadState).values(
            leadgen_id=leadgen_id,
            freshsales_record_id=freshsales_record_id,
            record_type=record_type,
        )
        if self.session.bind and self.session.bind.dialect.name == "sqlite":
            stmt = stmt.prefix_with("OR IGNORE")
        await self.session.execute(stmt)
        await self.session.commit()

    async def get_leadgen_id_by_record_id(self, record_id: str, record_type: str = "contact") -> str | None:
        result = await self.session.execute(
            select(LeadState.leadgen_id).where(LeadState.freshsales_record_id == record_id, LeadState.record_type == record_type)
        )
        return result.scalar_one_or_none()

    async def has_record(self, record_id: str, record_type: str) -> bool:
        result = await self.session.execute(
            select(LeadState.id).where(LeadState.freshsales_record_id == record_id, LeadState.record_type == record_type)
        )
        return result.scalar_one_or_none() is not None

    async def dead_letter(
        self,
        correlation_id: str,
        flow: str,
        payload: dict[str, object],
        error: str,
        attempt_count: int,
    ) -> None:
        await self.session.execute(
            insert(DeadLetter).values(
                correlation_id=correlation_id,
                flow=flow,
                payload=json.dumps(payload, default=str),
                error=error[:4000],
                attempt_count=attempt_count,
            )
        )
        await self.session.commit()


def meta_dedupe_key(leadgen_id: str) -> str:
    return f"meta_lead_capture:{leadgen_id}"


def freshsales_dedupe_key(record_id: str, status: str, occurred_window: str) -> str:
    return f"freshsales_status:{record_id}:{status}:{occurred_window}"
