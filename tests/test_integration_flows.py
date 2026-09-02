import hashlib
import hmac
import json

import pytest
from httpx import ASGITransport, AsyncClient, MockTransport, Response

from app.config import get_settings
from app.main import app
from app.services.freshsales_client import FreshsalesClient


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(b"appsecret", body, hashlib.sha256).hexdigest()


def meta_webhook_body(leadgen_id: str) -> bytes:
    return (
        '{"object":"page","entry":[{"id":"p","time":1,"changes":[{"field":"leadgen","value":{"leadgen_id":"'
        + leadgen_id
        + '"}}]}]}'
    ).encode()


@pytest.mark.asyncio
async def test_freshsales_lookup_search_returns_first_normalized_phone_match() -> None:
    settings = get_settings()

    async def handler(request):
        assert request.url.path.endswith("/api/lookup")
        assert request.url.params["q"] == "+91 93071-52210"
        assert request.url.params["f"] == "contacts"
        assert request.url.params["entities"] == "contact"
        return Response(
            200,
            json={
                "records": [
                    {"id": "402216158144", "mobile_number": "9307152210"},
                    {"id": "402216158145", "mobile_number": "+919307152210"},
                ]
            },
        )

    async with AsyncClient(transport=MockTransport(handler), base_url="http://test") as http_client:
        freshsales = FreshsalesClient(settings, http_client)
        matches = await freshsales.search_contacts_by_field("mobile_number", "+91 93071-52210")

    assert matches
    assert matches[0]["id"] == "402216158144"


@pytest.mark.asyncio
async def test_meta_new_lead_creates_freshsales_lead() -> None:
    settings = get_settings()
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
                if request.url.path.endswith("/api/lookup"):
                    calls["search"] += 1
                    return Response(200, json={"contacts": {"records": []}})
                if request.url.path.endswith("/api/contacts") and request.method == "POST":
                    calls["create"] += 1
                    created_payload.update(json.loads(request.content))
                    return Response(200, json={"contact": {"id": "lead-new"}})
                return Response(200, json={})

            app.state.http_client._transport.handle_async_request = handler  # type: ignore[attr-defined,method-assign]
            assert (await client.post("/webhooks/meta/leadgen", content=body, headers={"X-Hub-Signature-256": sign(body)})).status_code == 200
            assert calls == {"search": 1, "create": 1}
            assert created_payload["contact"]["email"] == "ada@example.com"  # type: ignore[index]
            assert created_payload["contact"]["first_name"] == "Ada"  # type: ignore[index]
            assert created_payload["contact"]["last_name"] == "Lovelace"  # type: ignore[index]
            assert created_payload["contact"]["custom_field"][settings.freshsales_meta_lead_field] == "l-new"  # type: ignore[index]


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
                if request.url.path.endswith("/api/lookup"):
                    return Response(200, json={"contacts": {"records": []}})
                if request.url.path.endswith("/api/contacts") and request.method == "POST":
                    calls["fresh_create"] += 1
                    return Response(200, json={"contact": {"id": "lead1"}})
                return Response(200, json={})

            app.state.http_client._transport.handle_async_request = handler  # type: ignore[attr-defined,method-assign]
            assert (await client.post("/webhooks/meta/leadgen", content=body, headers={"X-Hub-Signature-256": sign(body)})).status_code == 200
            assert (await client.post("/webhooks/meta/leadgen", content=body, headers={"X-Hub-Signature-256": sign(body)})).status_code == 200
            assert calls["fresh_create"] == 1


@pytest.mark.asyncio
async def test_meta_duplicate_mobile_create_safety_net_dead_letters_unresolved_duplicate() -> None:
    body = meta_webhook_body("l-mobile-race")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            calls = {"create": 0, "lookup": 0, "update": 0}
            async def handler(request):
                if "graph.facebook.com" in str(request.url) and request.method == "GET":
                    return Response(
                        200,
                        json={
                            "field_data": [
                                {"name": "full_name", "values": ["Ada Lovelace"]},
                                {"name": "email", "values": ["ada@example.com"]},
                                {"name": "phone_number", "values": ["+919307152210"]},
                            ]
                        },
                    )
                if request.url.path.endswith("/api/lookup"):
                    calls["lookup"] += 1
                    return Response(200, json={"contacts": {"records": []}})
                if request.url.path.endswith("/api/contacts") and request.method == "POST":
                    calls["create"] += 1
                    return Response(
                        400,
                        json={
                            "errors": {
                                "code": 400,
                                "message": [
                                    "The mobile number already exists.",
                                    'Mcr error {"error_code"=>3002, "errors"=>[{"object_type"=>"Contact", "object_id"=>"2090674640783768576", "field"=>"mobile_number", "code"=>3032, "message"=>"MobileNumber is not unique"}]}',
                                ],
                            }
                        },
                    )
                if request.url.path.endswith("/api/contacts/2090674640783768576") and request.method == "PUT":
                    calls["update"] += 1
                    return Response(200, json={})
                return Response(200, json={})

            app.state.http_client._transport.handle_async_request = handler  # type: ignore[attr-defined,method-assign]
            assert (await client.post("/webhooks/meta/leadgen", content=body, headers={"X-Hub-Signature-256": sign(body)})).status_code == 502
            assert calls == {"create": 1, "lookup": 1, "update": 0}


@pytest.mark.asyncio
async def test_meta_existing_lead_by_email_updates_meta_id_without_create() -> None:
    body = meta_webhook_body("l-email")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            calls = {"create": 0, "update": 0}

            async def handler(request):
                if "graph.facebook.com" in str(request.url) and request.method == "GET":
                    return Response(200, json={"field_data": [{"name": "email", "values": ["a@example.com"]}, {"name": "full_name", "values": ["Ada Lovelace"]}]})
                if request.url.path.endswith("/api/lookup"):
                    return Response(200, json={"contacts": {"records": [{"id": "lead-email", "email": "a@example.com", "first_name": "Ada"}]}})
                if request.url.path.endswith("/api/contacts/lead-email") and request.method == "PUT":
                    calls["update"] += 1
                    return Response(200, json={})
                if request.url.path.endswith("/api/contacts") and request.method == "POST":
                    calls["create"] += 1
                    return Response(200, json={"contact": {"id": "unexpected"}})
                return Response(200, json={})

            app.state.http_client._transport.handle_async_request = handler  # type: ignore[attr-defined,method-assign]
            assert (await client.post("/webhooks/meta/leadgen", content=body, headers={"X-Hub-Signature-256": sign(body)})).status_code == 200
            assert calls == {"create": 0, "update": 1}


@pytest.mark.asyncio
async def test_meta_phone_only_lead_creates_without_phone_prelookup() -> None:
    body = meta_webhook_body("l-phone")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            calls = {"create": 0, "update": 0}

            async def handler(request):
                if "graph.facebook.com" in str(request.url) and request.method == "GET":
                    return Response(200, json={"field_data": [{"name": "phone_number", "values": ["123"]}]})
                if request.url.path.endswith("/api/lookup"):
                    return Response(200, json={"contacts": {"records": [{"id": "lead-phone"}]}})
                if request.url.path.endswith("/api/contacts/lead-phone") and request.method == "PUT":
                    calls["update"] += 1
                    return Response(200, json={})
                if request.url.path.endswith("/api/contacts") and request.method == "POST":
                    calls["create"] += 1
                    return Response(200, json={"contact": {"id": "unexpected"}})
                return Response(200, json={})

            app.state.http_client._transport.handle_async_request = handler  # type: ignore[attr-defined,method-assign]
            assert (await client.post("/webhooks/meta/leadgen", content=body, headers={"X-Hub-Signature-256": sign(body)})).status_code == 200
            assert calls == {"create": 1, "update": 0}


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
                if request.url.path.endswith("/api/lookup"):
                    calls["search"] += 1
                    return Response(200, json={"contacts": {"records": []}})
                if request.url.path.endswith("/api/contacts") and request.method == "POST":
                    calls["create"] += 1
                    return Response(200, json={"contact": {"id": "lead-name"}})
                return Response(200, json={})

            app.state.http_client._transport.handle_async_request = handler  # type: ignore[attr-defined,method-assign]
            assert (await client.post("/webhooks/meta/leadgen", content=body, headers={"X-Hub-Signature-256": sign(body)})).status_code == 200
            assert calls == {"search": 0, "create": 1}


@pytest.mark.asyncio
async def test_freshsales_duplicate_flow() -> None:
    settings = get_settings()
    body = {
        "record_type": "contact",
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
            capi_payload: dict[str, object] = {}

            async def handler(request):
                if request.url.path.endswith("/api/contacts/lead1"):
                    return Response(
                        200,
                        json={
                            "contact": {
                                "email": "Ada@Example.COM ",
                                "mobile_number": "+91 93071-52210",
                                "custom_field": {settings.freshsales_meta_lead_field: "l1"},
                            }
                        },
                    )
                if "graph.facebook.com" in str(request.url) and request.method == "POST":
                    calls["capi"] += 1
                    capi_payload.update(json.loads(request.content))
                    return Response(200, json={"events_received": 1})
                return Response(200, json={})

            app.state.http_client._transport.handle_async_request = handler  # type: ignore[attr-defined,method-assign]
            headers = {"X-Freshsales-Signature": "freshsecret"}
            assert (await client.post("/webhooks/freshsales/status", json=body, headers=headers)).status_code == 200
            assert (await client.post("/webhooks/freshsales/status", json=body, headers=headers)).status_code == 200
            assert calls["capi"] == 1
            event = capi_payload["data"][0]  # type: ignore[index]
            assert event["event_id"] == "freshsales_status:lead1:CC Qualified:202608101000"  # type: ignore[index]
            assert event["user_data"]["lead_id"] == "l1"  # type: ignore[index]
            assert event["user_data"]["em"] == [hashlib.sha256("ada@example.com".encode()).hexdigest()]  # type: ignore[index]
            assert event["user_data"]["ph"] == [hashlib.sha256("919307152210".encode()).hexdigest()]  # type: ignore[index]
