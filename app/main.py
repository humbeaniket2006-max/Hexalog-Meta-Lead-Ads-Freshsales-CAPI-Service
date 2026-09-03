from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

import httpx
from fastapi import FastAPI

from app.config import get_settings
from app.db.models import Base
from app.db.repository import Repository
from app.db.session import build_engine, build_sessionmaker
from app.logging_config import configure_logging
from app.routes.freshsales_lead_events import router as freshsales_lead_events_router
from app.security import InMemoryRateLimiter
from app.webhooks.freshsales_status import router as freshsales_router
from app.webhooks.meta_lead_capture import router as meta_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    engine = build_engine(settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    app.state.engine = engine
    app.state.sessionmaker = build_sessionmaker(engine)
    app.state.http_client = httpx.AsyncClient()
    app.state.rate_limiter = InMemoryRateLimiter(settings.rate_limit_per_minute)
    try:
        yield
    finally:
        await app.state.http_client.aclose()
        await engine.dispose()


app = FastAPI(title="Hexalog Freshsales CAPI Service", lifespan=lifespan)
app.include_router(meta_router)
app.include_router(freshsales_router)
app.include_router(freshsales_lead_events_router)


@app.get("/health")
async def health() -> dict[str, str]:
    async with app.state.sessionmaker() as session:
        await Repository(session).health()
    return {"status": "ok"}
