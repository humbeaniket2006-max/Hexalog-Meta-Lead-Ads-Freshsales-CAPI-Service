import pytest

from app.retry import retry_async


@pytest.mark.asyncio
async def test_retry_succeeds_after_transient(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("asyncio.sleep", no_sleep)

    async def op() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            import httpx

            raise httpx.TimeoutException("timeout")
        return "ok"

    assert await retry_async(op, 2) == "ok"
    assert calls == 2
