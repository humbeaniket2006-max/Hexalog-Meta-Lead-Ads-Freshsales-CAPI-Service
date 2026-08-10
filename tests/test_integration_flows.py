import hashlib
import hmac

import pytest
from httpx import ASGITransport, AsyncClient, Response

from app.config import get_settings
from app.main import app


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(b"appsecret", body, hashlib.sha256).hexdigest()


@pytest.mark.asyncio
async def test_meta_duplicate_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("asyncio.sleep", no_sleep)
    body = b'{"object":"page","entry":[{"id":"p","time":1,"changes":[{"field":"leadgen","value":{"leadgen_id":"l1"}}]}]}'
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            calls = {"fresh_update": 0}

            async def handler(request):
                if "graph.facebook.com" in str(request.url) and request.method == "GET":
                    return Response(200, json={"field_data": [{"name": "email", "values": ["a@example.com"]}]})
                if request.url.path.endswith("/api/leads/search"):
                    return Response(200, json={"leads": [{"id": "lead1"}]})
                if request.url.path.endswith("/api/leads/lead1"):
                    calls["fresh_update"] += 1
                    return Response(200, json={})
                return Response(200, json={})

            app.state.http_client._transport.handle_async_request = handler  # type: ignore[attr-defined,method-assign]
            assert (await client.post("/webhooks/meta/leadgen", content=body, headers={"X-Hub-Signature-256": sign(body)})).status_code == 200
            assert (await client.post("/webhooks/meta/leadgen", content=body, headers={"X-Hub-Signature-256": sign(body)})).status_code == 200
            assert calls["fresh_update"] == 1


@pytest.mark.asyncio
async def test_freshsales_duplicate_flow() -> None:
    settings = get_settings()
    body = {
        "record_type": "lead",
        "record_id": "lead1",
        "status": "CC Qualified",
        "occurred_at": "2026-08-10T10:00:00Z",
    }
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            from app.db.repository import Repository

            async with app.state.sessionmaker() as session:
                await Repository(session).save_lead_state("l1", "lead1")
            calls = {"capi": 0}

            async def handler(request):
                if request.url.path.endswith("/api/leads/lead1"):
                    return Response(200, json={"lead": {"custom_field": {settings.freshsales_meta_lead_field: "l1"}}})
                if "graph.facebook.com" in str(request.url) and request.method == "POST":
                    calls["capi"] += 1
                    return Response(200, json={"events_received": 1})
                return Response(200, json={})

            app.state.http_client._transport.handle_async_request = handler  # type: ignore[attr-defined,method-assign]
            headers = {"X-Freshsales-Signature": "freshsecret"}
            assert (await client.post("/webhooks/freshsales/status", json=body, headers=headers)).status_code == 200
            assert (await client.post("/webhooks/freshsales/status", json=body, headers=headers)).status_code == 200
            assert calls["capi"] == 1
