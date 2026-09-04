from collections.abc import AsyncIterator

from sqlalchemy.engine.url import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings


def normalize_async_database_url(database_url: str) -> str:
    if database_url.startswith("postgres://"):
        return database_url.replace("postgres://", "postgresql+asyncpg://", 1)
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return database_url


def build_engine(settings: Settings) -> AsyncEngine:
    """Build the async engine, translating libpq sslmode for asyncpg.

    Supabase requires SSL, but asyncpg does not accept libpq-style
    ``sslmode`` query parameters in the URL. Strip supported Supabase SSL
    modes from Postgres URLs and pass them through asyncpg's ``ssl`` argument.
    """
    database_url = normalize_async_database_url(settings.database_url)
    engine_kwargs: dict[str, object] = {"pool_pre_ping": True}

    parsed_url = make_url(database_url)
    if parsed_url.drivername == "postgresql+asyncpg":
        sslmode = parsed_url.query.get("sslmode")
        if isinstance(sslmode, tuple):
            sslmode = sslmode[-1]
        if sslmode in {"require", "verify-full"}:
            database_url = parsed_url.difference_update_query(["sslmode"]).render_as_string(
                hide_password=False
            )
            engine_kwargs["connect_args"] = {"ssl": sslmode}

    return create_async_engine(database_url, **engine_kwargs)


def build_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    from app.main import app

    sessionmaker = app.state.sessionmaker
    async with sessionmaker() as session:
        yield session
