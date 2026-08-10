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
