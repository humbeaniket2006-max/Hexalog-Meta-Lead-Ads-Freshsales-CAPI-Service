import hashlib
import hmac
import json

import pytest
from httpx import ASGITransport, AsyncClient, Response

from app.config import get_settings
from app.main import app


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(b"appsecret", body, hashlib.sha256).hexdigest()


def meta_webhook_body(leadgen_id: str) -> bytes:
    return (
        '{"object":"page","entry":[{"id":"p","time":1,"changes":[{"field":"leadgen","value":{"leadgen_id":"'
        + leadgen_id
        + '"}}]}]}'
    ).encode()


@pytest.mark.asyncio
async def test_meta_new_lead_creates_freshsales_lead() -> None:
    body = meta_webhook_body("l-new")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            calls = {"search": 0, "create": 0}
            created_payload: dict[str, object] = {}

            async def handler(request):
                if "graph.facebook.com" in str(request.url) and request.method == "GET":
                    return Response(
                        200,
                        json={
                            "field_data": [
                                {"name": "full_name", "values": ["Ada Lovelace"]},
                                {"name": "email", "values": ["ada@example.com"]},
                                {"name": "phone_number", "values": ["123"]},
                            ]
                        },
                    )
                if request.url.path.endswith("/api/leads/search"):
                    calls["search"] += 1
                    return Response(200, json={"leads": []})
                if request.url.path.endswith("/api/leads") and request.method == "POST":
                    calls["create"] += 1
                    created_payload.update(json.loads(request.content))
                    return Response(200, json={"lead": {"id": "lead-new"}})
                return Response(200, json={})

            app.state.http_client._transport.handle_async_request = handler  # type: ignore[attr-defined,method-assign]
            assert (await client.post("/webhooks/meta/leadgen", content=body, headers={"X-Hub-Signature-256": sign(body)})).status_code == 200
            assert calls == {"search": 2, "create": 1}
            assert created_payload["lead"]["email"] == "ada@example.com"  # type: ignore[index]
            assert created_payload["lead"]["first_name"] == "Ada"  # type: ignore[index]
            assert created_payload["lead"]["last_name"] == "Lovelace"  # type: ignore[index]
            assert created_payload["lead"]["custom_field"]["meta_lead_id"] == "l-new"  # type: ignore[index]


@pytest.mark.asyncio
async def test_meta_duplicate_flow() -> None:
    body = meta_webhook_body("l1")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            calls = {"fresh_create": 0}

            async def handler(request):
                if "graph.facebook.com" in str(request.url) and request.method == "GET":
                    return Response(200, json={"field_data": [{"name": "email", "values": ["a@example.com"]}]})
                if request.url.path.endswith("/api/leads/search"):
                    return Response(200, json={"leads": []})
                if request.url.path.endswith("/api/leads") and request.method == "POST":
                    calls["fresh_create"] += 1
                    return Response(200, json={"lead": {"id": "lead1"}})
                return Response(200, json={})

            app.state.http_client._transport.handle_async_request = handler  # type: ignore[attr-defined,method-assign]
            assert (await client.post("/webhooks/meta/leadgen", content=body, headers={"X-Hub-Signature-256": sign(body)})).status_code == 200
            assert (await client.post("/webhooks/meta/leadgen", content=body, headers={"X-Hub-Signature-256": sign(body)})).status_code == 200
            assert calls["fresh_create"] == 1


@pytest.mark.asyncio
async def test_meta_existing_lead_by_email_updates_without_duplicate() -> None:
    body = meta_webhook_body("l-email")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            calls = {"create": 0, "update": 0}
            updated_payload: dict[str, object] = {}

            async def handler(request):
                if "graph.facebook.com" in str(request.url) and request.method == "GET":
                    return Response(200, json={"field_data": [{"name": "email", "values": ["a@example.com"]}, {"name": "full_name", "values": ["Ada Lovelace"]}]})
                if request.url.path.endswith("/api/leads/search"):
                    return Response(200, json={"leads": [{"id": "lead-email", "email": "a@example.com", "first_name": "Ada"}]})
                if request.url.path.endswith("/api/leads/lead-email") and request.method == "PUT":
                    calls["update"] += 1
                    updated_payload.update(json.loads(request.content))
                    return Response(200, json={})
                if request.url.path.endswith("/api/leads") and request.method == "POST":
                    calls["create"] += 1
                    return Response(200, json={"lead": {"id": "unexpected"}})
                return Response(200, json={})

            app.state.http_client._transport.handle_async_request = handler  # type: ignore[attr-defined,method-assign]
            assert (await client.post("/webhooks/meta/leadgen", content=body, headers={"X-Hub-Signature-256": sign(body)})).status_code == 200
            assert calls == {"create": 0, "update": 1}
            assert "first_name" not in updated_payload["lead"]  # type: ignore[operator]
            assert updated_payload["lead"]["last_name"] == "Lovelace"  # type: ignore[index]
            assert updated_payload["lead"]["custom_field"]["meta_lead_id"] == "l-email"  # type: ignore[index]


@pytest.mark.asyncio
async def test_meta_existing_lead_by_phone_updates_without_duplicate() -> None:
    body = meta_webhook_body("l-phone")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            searched: list[str] = []
            calls = {"create": 0, "update": 0}

            async def handler(request):
                if "graph.facebook.com" in str(request.url) and request.method == "GET":
                    return Response(200, json={"field_data": [{"name": "phone_number", "values": ["123"]}]})
                if request.url.path.endswith("/api/leads/search"):
                    searched.append(str(request.url))
                    return Response(200, json={"leads": [{"id": "lead-phone"}]})
                if request.url.path.endswith("/api/leads/lead-phone") and request.method == "PUT":
                    calls["update"] += 1
                    return Response(200, json={})
                if request.url.path.endswith("/api/leads") and request.method == "POST":
                    calls["create"] += 1
                    return Response(200, json={"lead": {"id": "unexpected"}})
                return Response(200, json={})

            app.state.http_client._transport.handle_async_request = handler  # type: ignore[attr-defined,method-assign]
            assert (await client.post("/webhooks/meta/leadgen", content=body, headers={"X-Hub-Signature-256": sign(body)})).status_code == 200
            assert calls == {"create": 0, "update": 1}
            assert any("phone=123" in url for url in searched)


@pytest.mark.asyncio
async def test_meta_name_only_payload_creates_low_confidence_lead() -> None:
    body = meta_webhook_body("l-name")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            calls = {"search": 0, "create": 0}

            async def handler(request):
                if "graph.facebook.com" in str(request.url) and request.method == "GET":
                    return Response(200, json={"field_data": [{"name": "full_name", "values": ["Single Name"]}]})
                if request.url.path.endswith("/api/leads/search"):
                    calls["search"] += 1
                    return Response(200, json={"leads": []})
                if request.url.path.endswith("/api/leads") and request.method == "POST":
                    calls["create"] += 1
                    return Response(200, json={"lead": {"id": "lead-name"}})
                return Response(200, json={})

            app.state.http_client._transport.handle_async_request = handler  # type: ignore[attr-defined,method-assign]
            assert (await client.post("/webhooks/meta/leadgen", content=body, headers={"X-Hub-Signature-256": sign(body)})).status_code == 200
            assert calls == {"search": 0, "create": 1}


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
