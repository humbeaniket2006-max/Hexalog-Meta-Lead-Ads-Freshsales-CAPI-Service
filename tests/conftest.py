import pytest


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("META_VERIFY_TOKEN", "verify")
    monkeypatch.setenv("META_APP_SECRET", "appsecret")
    monkeypatch.setenv("META_PAGE_ACCESS_TOKEN", "pagetoken")
    monkeypatch.setenv("META_PIXEL_ID", "pixel123")
    monkeypatch.setenv("FRESHSALES_DOMAIN", "https://fresh.example.com")
    monkeypatch.setenv("FRESHSALES_API_KEY", "freshkey")
    monkeypatch.setenv("FRESHSALES_WEBHOOK_SECRET", "freshsecret")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("MATCH_RETRY_MIN_SECONDS", "0.1")
    monkeypatch.setenv("MATCH_RETRY_MAX_SECONDS", "0.1")
    from app.config import get_settings

    get_settings.cache_clear()
