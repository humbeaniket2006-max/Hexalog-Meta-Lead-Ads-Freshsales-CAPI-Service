import pytest

from app.config import get_settings
from app.matching import extract_field_data, match_with_backoff


def test_status_mapping() -> None:
    settings = get_settings()
    assert settings.event_mapping["CC Qualified"] == "Qualified"
    assert settings.event_mapping["Future Prospect"] == "Qualified"
    assert settings.event_mapping["Dropped"] == "Not Qualified"
    assert settings.event_mapping["Won"] == "Conversion"


def test_extract_field_data() -> None:
    email, phone = extract_field_data(
        {"field_data": [{"name": "email", "values": ["a@example.com"]}, {"name": "phone_number", "values": ["123"]}]}
    )
    assert email == "a@example.com"
    assert phone == "123"


@pytest.mark.asyncio
async def test_match_retry_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeFreshsales:
        def __init__(self) -> None:
            self.calls = 0

        async def find_lead_by_contact(self, email: str | None, phone: str | None) -> dict[str, object] | None:
            self.calls += 1
            return {"id": "lead1"} if self.calls == 2 else None

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("asyncio.sleep", no_sleep)
    settings = get_settings()
    freshsales = FakeFreshsales()
    result = await match_with_backoff(freshsales, settings, "a@example.com", None, "corr")  # type: ignore[arg-type]
    assert result == {"id": "lead1"}
    assert freshsales.calls == 2
