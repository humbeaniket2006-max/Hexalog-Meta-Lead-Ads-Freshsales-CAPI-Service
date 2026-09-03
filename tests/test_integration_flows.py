import hashlib
import hmac
import json

import pytest
from httpx import ASGITransport, AsyncClient, MockTransport, Response
from sqlalchemy import select

from app.config import get_settings
from app.db.models import DeadLetter
from app.db.repository import Repository
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


async def dead_letter_errors() -> list[str]:
    async with app.state.sessionmaker() as session:
        result = await session.execute(select(DeadLetter.error))
        return list(result.scalars())


@pytest.mark.asyncio
async def test_freshsales_lead_event_qualified_with_match_sends_meta_capi_event() -> None:
    body = {"event_name": "Qualified", "contact_id": "contact-qualified"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            async with app.state.sessionmaker() as session:
                await Repository(session).save_lead_state("leadgen-qualified", "contact-qualified")

            calls = {"capi": 0}
            capi_payload: dict[str, object] = {}

            async def handler(request):
                if "graph.facebook.com" in str(request.url) and request.method == "POST":
                    calls["capi"] += 1
                    assert request.url.path == "/v20.0/dataset123/events"
                    capi_payload.update(json.loads(request.content))
                    return Response(200, json={"events_received": 1})
                return Response(200, json={})

            app.state.http_client._transport.handle_async_request = handler  # type: ignore[attr-defined,method-assign]
            response = await client.post("/webhooks/freshsales/lead-event", json=body, headers={"X-Internal-Secret": "freshsecret"})

            assert response.status_code == 200
            assert calls["capi"] == 1
            assert capi_payload["access_token"] == "capitoken"
            event = capi_payload["data"][0]  # type: ignore[index]
            assert event["event_name"] == "Qualified"
            assert event["action_source"] == "system_generated"
            assert isinstance(event["event_time"], int)
            assert event["user_data"] == {"lead_id": "leadgen-qualified"}


@pytest.mark.asyncio
async def test_freshsales_lead_event_qualified_with_no_match_returns_ok_without_capi() -> None:
    body = {"event_name": "Qualified", "contact_id": "missing-contact"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            calls = {"capi": 0}

            async def handler(request):
                if "graph.facebook.com" in str(request.url) and request.method == "POST":
                    calls["capi"] += 1
                    return Response(200, json={"events_received": 1})
                return Response(200, json={})

            app.state.http_client._transport.handle_async_request = handler  # type: ignore[attr-defined,method-assign]
            response = await client.post("/webhooks/freshsales/lead-event", json=body, headers={"X-Internal-Secret": "freshsecret"})

            assert response.status_code == 200
            assert calls["capi"] == 0
            assert await dead_letter_errors() == []


@pytest.mark.asyncio
async def test_freshsales_lead_event_conversion_uses_payload_meta_lead_id_for_capi() -> None:
    body = {"event_name": "Conversion", "deal_id": "deal-won", "meta_lead_id": "leadgen-conversion", "amount": 2499.5}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            calls = {"capi": 0}
            capi_payload: dict[str, object] = {}

            async def handler(request):
                if "graph.facebook.com" in str(request.url) and request.method == "POST":
                    calls["capi"] += 1
                    assert request.url.path == "/v20.0/dataset123/events"
                    capi_payload.update(json.loads(request.content))
                    return Response(200, json={"events_received": 1})
                return Response(200, json={})

            app.state.http_client._transport.handle_async_request = handler  # type: ignore[attr-defined,method-assign]
            response = await client.post("/webhooks/freshsales/lead-event", json=body, headers={"X-Internal-Secret": "freshsecret"})

            assert response.status_code == 200
            assert calls["capi"] == 1
            assert capi_payload["access_token"] == "capitoken"
            event = capi_payload["data"][0]  # type: ignore[index]
            assert event["event_name"] == "Conversion"
            assert event["action_source"] == "system_generated"
            assert isinstance(event["event_time"], int)
            assert event["user_data"] == {"lead_id": "leadgen-conversion"}
            assert event["custom_data"] == {"value": 2499.5, "currency": "USD"}


@pytest.mark.asyncio
async def test_freshsales_lead_event_meta_capi_failure_dead_letters_and_returns_ok() -> None:
    body = {"event_name": "Qualified", "contact_id": "contact-meta-fail"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            async with app.state.sessionmaker() as session:
                await Repository(session).save_lead_state("leadgen-meta-fail", "contact-meta-fail")

            calls = {"capi": 0}

            async def handler(request):
                if "graph.facebook.com" in str(request.url) and request.method == "POST":
                    calls["capi"] += 1
                    return Response(500, json={"error": "downstream"})
                return Response(200, json={})

            app.state.http_client._transport.handle_async_request = handler  # type: ignore[attr-defined,method-assign]
            response = await client.post("/webhooks/freshsales/lead-event", json=body, headers={"X-Internal-Secret": "freshsecret"})

            assert response.status_code == 200
            assert calls["capi"] == get_settings().max_http_attempts
            assert await dead_letter_errors() == ["RetryExhaustedError"]


@pytest.mark.asyncio
async def test_freshsales_lead_event_deal_created_with_match_stamps_deal_meta_lead_id() -> None:
    body = {"event_name": "DealCreated", "contact_id": "contact-deal", "deal_id": "deal-created"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            async with app.state.sessionmaker() as session:
                await Repository(session).save_lead_state("leadgen-deal-created", "contact-deal")

            calls = {"update": 0, "capi": 0}
            update_payload: dict[str, object] = {}

            async def handler(request):
                if request.url.path.endswith("/api/deals/deal-created") and request.method == "PUT":
                    calls["update"] += 1
                    update_payload.update(json.loads(request.content))
                    return Response(200, json={})
                if "graph.facebook.com" in str(request.url) and request.method == "POST":
                    calls["capi"] += 1
                    return Response(200, json={"events_received": 1})
                return Response(200, json={})

            app.state.http_client._transport.handle_async_request = handler  # type: ignore[attr-defined,method-assign]
            response = await client.post("/webhooks/freshsales/lead-event", json=body, headers={"X-Internal-Secret": "freshsecret"})

            assert response.status_code == 200
            assert calls == {"update": 1, "capi": 0}
            assert update_payload == {"deal": {"custom_field": {"cf_meta_lead_id": "leadgen-deal-created"}}}


@pytest.mark.asyncio
async def test_freshsales_lead_event_deal_created_with_no_match_returns_ok_without_update() -> None:
    body = {"event_name": "DealCreated", "contact_id": "missing-contact", "deal_id": "deal-no-match"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            calls = {"update": 0, "capi": 0}

            async def handler(request):
                if request.url.path.endswith("/api/deals/deal-no-match") and request.method == "PUT":
                    calls["update"] += 1
                    return Response(200, json={})
                if "graph.facebook.com" in str(request.url) and request.method == "POST":
                    calls["capi"] += 1
                    return Response(200, json={"events_received": 1})
                return Response(200, json={})

            app.state.http_client._transport.handle_async_request = handler  # type: ignore[attr-defined,method-assign]
            response = await client.post("/webhooks/freshsales/lead-event", json=body, headers={"X-Internal-Secret": "freshsecret"})

            assert response.status_code == 200
            assert calls == {"update": 0, "capi": 0}
            assert await dead_letter_errors() == []


@pytest.mark.asyncio
async def test_freshsales_filtered_search_returns_first_normalized_phone_match() -> None:
    settings = get_settings()

    async def handler(request):
        assert request.url.path.endswith("/api/filtered_search/contact")
        payload = json.loads(request.content)
        assert payload == {"filter_rule": [{"attribute": "mobile_number", "operator": "is_in", "value": "+91 93071-52210"}]}
        return Response(
            200,
            json={
                "contacts": [
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
async def test_meta_new_lead_dead_letters_when_no_freshsales_match() -> None:
    settings = get_settings()
    body = meta_webhook_body("l-new")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            calls = {"search": 0, "create": 0}

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
                if request.url.path.endswith("/api/filtered_search/contact"):
                    calls["search"] += 1
                    return Response(200, json={"contacts": []})
                if request.url.path.endswith("/api/contacts") and request.method == "POST":
                    calls["create"] += 1
                    return Response(500, json={})
                return Response(200, json={})

            app.state.http_client._transport.handle_async_request = handler  # type: ignore[attr-defined,method-assign]
            assert (await client.post("/webhooks/meta/leadgen", content=body, headers={"X-Hub-Signature-256": sign(body)})).status_code == 502
            assert calls == {"search": settings.match_retry_attempts * 2, "create": 0}
            assert await dead_letter_errors() == ["FreshsalesContactCreateError"]


@pytest.mark.asyncio
async def test_meta_duplicate_flow_dead_letters_unmatched_lead_without_create() -> None:
    settings = get_settings()
    body = meta_webhook_body("l1")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            calls = {"search": 0, "fresh_create": 0}

            async def handler(request):
                if "graph.facebook.com" in str(request.url) and request.method == "GET":
                    return Response(200, json={"field_data": [{"name": "email", "values": ["a@example.com"]}]})
                if request.url.path.endswith("/api/lookup"):
                    calls["search"] += 1
                    return Response(200, json={"contacts": {"records": []}})
                if request.url.path.endswith("/api/contacts") and request.method == "POST":
                    calls["fresh_create"] += 1
                    return Response(500, json={})
                return Response(200, json={})

            app.state.http_client._transport.handle_async_request = handler  # type: ignore[attr-defined,method-assign]
            assert (await client.post("/webhooks/meta/leadgen", content=body, headers={"X-Hub-Signature-256": sign(body)})).status_code == 502
            assert calls == {"search": settings.match_retry_attempts, "fresh_create": 0}
            assert await dead_letter_errors() == ["FreshsalesContactCreateError"]


@pytest.mark.asyncio
async def test_meta_duplicate_mobile_miss_dead_letters_without_create() -> None:
    settings = get_settings()
    body = meta_webhook_body("l-mobile-race")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            calls = {"create": 0, "search": 0, "update": 0}
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
                    calls["search"] += 1
                    return Response(200, json={"contacts": {"records": []}})
                if request.url.path.endswith("/api/filtered_search/contact"):
                    calls["search"] += 1
                    return Response(200, json={"contacts": {"records": []}})
                if request.url.path.endswith("/api/contacts") and request.method == "POST":
                    calls["create"] += 1
                    return Response(500, json={})
                if request.url.path.endswith("/api/contacts/2090674640783768576") and request.method == "PUT":
                    calls["update"] += 1
                    return Response(200, json={})
                return Response(200, json={})

            app.state.http_client._transport.handle_async_request = handler  # type: ignore[attr-defined,method-assign]
            assert (await client.post("/webhooks/meta/leadgen", content=body, headers={"X-Hub-Signature-256": sign(body)})).status_code == 502
            assert calls == {"create": 0, "search": settings.match_retry_attempts * 2, "update": 0}
            assert await dead_letter_errors() == ["FreshsalesContactCreateError"]


@pytest.mark.asyncio
async def test_meta_match_with_backoff_retries_before_giving_up(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    body = meta_webhook_body("l-retry-miss")
    transport = ASGITransport(app=app)

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("app.matching.asyncio.sleep", no_sleep)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            calls = {"lookup": 0, "create": 0}

            async def handler(request):
                if "graph.facebook.com" in str(request.url) and request.method == "GET":
                    return Response(200, json={"field_data": [{"name": "email", "values": ["retry@example.com"]}]})
                if request.url.path.endswith("/api/lookup"):
                    calls["lookup"] += 1
                    return Response(200, json={"contacts": {"records": []}})
                if request.url.path.endswith("/api/contacts") and request.method == "POST":
                    calls["create"] += 1
                    return Response(500, json={})
                return Response(200, json={})

            app.state.http_client._transport.handle_async_request = handler  # type: ignore[attr-defined,method-assign]
            assert (await client.post("/webhooks/meta/leadgen", content=body, headers={"X-Hub-Signature-256": sign(body)})).status_code == 502
            assert calls["lookup"] == settings.match_retry_attempts
            assert calls["lookup"] > 1
            assert calls["create"] == 0
            assert await dead_letter_errors() == ["FreshsalesContactCreateError"]


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
async def test_meta_phone_only_lead_updates_meta_id_without_create() -> None:
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
                if request.url.path.endswith("/api/filtered_search/contact"):
                    return Response(200, json={"contacts": [{"id": "lead-phone", "mobile_number": "123"}]})
                if request.url.path.endswith("/api/contacts/lead-phone") and request.method == "PUT":
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
async def test_meta_name_only_payload_dead_letters_without_create() -> None:
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
                if request.url.path.endswith("/api/filtered_search/contact"):
                    calls["search"] += 1
                    return Response(200, json={"contacts": []})
                if request.url.path.endswith("/api/contacts") and request.method == "POST":
                    calls["create"] += 1
                    return Response(500, json={})
                return Response(200, json={})

            app.state.http_client._transport.handle_async_request = handler  # type: ignore[attr-defined,method-assign]
            assert (await client.post("/webhooks/meta/leadgen", content=body, headers={"X-Hub-Signature-256": sign(body)})).status_code == 502
            assert calls == {"search": 0, "create": 0}
            assert await dead_letter_errors() == ["FreshsalesContactCreateError"]


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
