from types import SimpleNamespace

from app.db import session as db_session
from app.db.session import normalize_async_database_url


def test_normalize_render_postgres_url_for_asyncpg() -> None:
    assert (
        normalize_async_database_url("postgres://user:pass@example.com:5432/db")
        == "postgresql+asyncpg://user:pass@example.com:5432/db"
    )
    assert (
        normalize_async_database_url("postgresql://user:pass@example.com:5432/db")
        == "postgresql+asyncpg://user:pass@example.com:5432/db"
    )


def test_normalize_database_url_keeps_async_urls() -> None:
    assert normalize_async_database_url("postgresql+asyncpg://user:pass@example.com/db") == (
        "postgresql+asyncpg://user:pass@example.com/db"
    )
    assert normalize_async_database_url("sqlite+aiosqlite:///:memory:") == "sqlite+aiosqlite:///:memory:"


def test_build_engine_strips_sslmode_require_for_asyncpg(monkeypatch) -> None:
    captured: dict[str, object] = {}
    expected_engine = object()

    def fake_create_async_engine(url: str, **kwargs: object) -> object:
        captured["url"] = url
        captured["kwargs"] = kwargs
        return expected_engine

    monkeypatch.setattr(db_session, "create_async_engine", fake_create_async_engine)

    engine = db_session.build_engine(
        SimpleNamespace(
            database_url="postgres://user:pass@example.com:5432/db?sslmode=require&foo=bar"
        )
    )

    assert engine is expected_engine
    assert captured["url"] == "postgresql+asyncpg://user:pass@example.com:5432/db?foo=bar"
    assert captured["kwargs"] == {
        "pool_pre_ping": True,
        "connect_args": {"ssl": "require"},
    }
