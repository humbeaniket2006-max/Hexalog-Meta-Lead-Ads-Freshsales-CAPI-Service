import pytest

from app.config import get_settings
from app.db.repository import Repository
from app.db.session import build_engine, build_sessionmaker


@pytest.mark.asyncio
async def test_dedupe_acquire_success_and_retry_after_failure() -> None:
    engine = build_engine(get_settings())
    sessionmaker = build_sessionmaker(engine)
    async with sessionmaker() as session:
        repo = Repository(session)
        await repo.init_schema()
        assert await repo.acquire_dedupe("k")
        assert not await repo.acquire_dedupe("k")
        await repo.mark_dedupe_failed("k")
        assert await repo.acquire_dedupe("k")
        await repo.mark_dedupe_success("k")
        assert not await repo.acquire_dedupe("k")
    await engine.dispose()
