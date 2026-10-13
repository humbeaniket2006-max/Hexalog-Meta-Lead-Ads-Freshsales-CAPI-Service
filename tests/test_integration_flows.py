import hashlib
import hmac
import json

import pytest
from httpx import ASGITransport, AsyncClient, MockTransport, Response
from sqlalchemy import select

from app.config import get_settings
from app.db.models import DeadLetter, DedupeEvent
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
    body = {"event_name": "DealCreated", "deal_id": "deal-created"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            async with app.state.sessionmaker() as session:
                await Repository(session).save_lead_state("leadgen-deal-created", "contact-deal")

            calls = {"get_deal": 0, "update": 0, "capi": 0}
            update_payload: dict[str, object] = {}

            async def handler(request):
                if request.url.path.endswith("/api/deals/deal-created") and request.method == "GET":
                    calls["get_deal"] += 1
                    return Response(200, json={"deal": {"id": "deal-created", "contacts": [{"id": "contact-deal"}]}})
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
            assert calls == {"get_deal": 1, "update": 1, "capi": 0}
            assert update_payload == {"deal": {"custom_field": {"cf_meta_lead_id": "leadgen-deal-created"}}}


@pytest.mark.asyncio
async def test_freshsales_lead_event_deal_created_with_no_contacts_returns_ok_without_lookup_or_update(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    body = {"event_name": "DealCreated", "deal_id": "deal-no-contacts"}
    transport = ASGITransport(app=app)

    async def fail_lookup(*_: object, **__: object) -> str | None:
        raise AssertionError("leadgen lookup should not run without linked contacts")

    monkeypatch.setattr(Repository, "get_leadgen_id_by_record_id", fail_lookup)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            calls = {"get_deal": 0, "update": 0, "capi": 0}

            async def handler(request):
                if request.url.path.endswith("/api/deals/deal-no-contacts") and request.method == "GET":
                    calls["get_deal"] += 1
                    return Response(200, json={"deal": {"id": "deal-no-contacts", "contacts": []}})
                if request.url.path.endswith("/api/deals/deal-no-contacts") and request.method == "PUT":
                    calls["update"] += 1
                    return Response(200, json={})
                if "graph.facebook.com" in str(request.url) and request.method == "POST":
                    calls["capi"] += 1
                    return Response(200, json={"events_received": 1})
                return Response(200, json={})

            app.state.http_client._transport.handle_async_request = handler  # type: ignore[attr-defined,method-assign]
            response = await client.post("/webhooks/freshsales/lead-event", json=body, headers={"X-Internal-Secret": "freshsecret"})

            assert response.status_code == 200
            assert calls == {"get_deal": 1, "update": 0, "capi": 0}
            assert await dead_letter_errors() == []
            assert "freshsales_deal_no_linked_contact" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_freshsales_lead_event_deal_created_with_contact_no_leadgen_returns_ok_without_update(
    capsys: pytest.CaptureFixture[str],
) -> None:
    body = {"event_name": "DealCreated", "deal_id": "deal-orphan-contact"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            calls = {"get_deal": 0, "update": 0, "capi": 0}

            async def handler(request):
                if request.url.path.endswith("/api/deals/deal-orphan-contact") and request.method == "GET":
                    calls["get_deal"] += 1
                    return Response(200, json={"deal": {"id": "deal-orphan-contact", "contacts": [{"id": "orphan-contact"}]}})
                if request.url.path.endswith("/api/deals/deal-orphan-contact") and request.method == "PUT":
                    calls["update"] += 1
                    return Response(200, json={})
                if "graph.facebook.com" in str(request.url) and request.method == "POST":
                    calls["capi"] += 1
                    return Response(200, json={"events_received": 1})
                return Response(200, json={})

            app.state.http_client._transport.handle_async_request = handler  # type: ignore[attr-defined,method-assign]
            response = await client.post("/webhooks/freshsales/lead-event", json=body, headers={"X-Internal-Secret": "freshsecret"})

            assert response.status_code == 200
            assert calls == {"get_deal": 1, "update": 0, "capi": 0}
            assert await dead_letter_errors() == []
            assert "freshsales_lead_event_no_match" in capsys.readouterr().out


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
async def test_meta_new_lead_creates_contact_when_no_freshsales_match() -> None:
    settings = get_settings()
    body = meta_webhook_body("l-new")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            calls = {"search": 0, "create": 0}
            created_payloads: list[dict] = []

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
                    created_payloads.append(json.loads(request.content))
                    return Response(200, json={"contact": {"id": "created-1"}})
                return Response(200, json={})

            app.state.http_client._transport.handle_async_request = handler  # type: ignore[attr-defined,method-assign]
            assert (await client.post("/webhooks/meta/leadgen", content=body, headers={"X-Hub-Signature-256": sign(body)})).status_code == 200
            assert calls == {"search": settings.match_retry_attempts * 2, "create": 1}
            assert created_payloads[0]["contact"]["custom_field"][settings.freshsales_meta_lead_field] == "l-new"
            assert await dead_letter_errors() == []
            async with app.state.sessionmaker() as session:
                assert await Repository(session).get_leadgen_id_by_record_id("created-1") == "l-new"


@pytest.mark.asyncio
async def test_meta_unmatched_email_lead_creates_contact() -> None:
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
                    return Response(200, json={"contact": {"id": "created-2"}})
                return Response(200, json={})

            app.state.http_client._transport.handle_async_request = handler  # type: ignore[attr-defined,method-assign]
            assert (await client.post("/webhooks/meta/leadgen", content=body, headers={"X-Hub-Signature-256": sign(body)})).status_code == 200
            assert calls == {"search": settings.match_retry_attempts, "fresh_create": 1}
            assert await dead_letter_errors() == []


@pytest.mark.asyncio
async def test_meta_duplicate_mobile_on_create_updates_existing_contact() -> None:
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
                    return Response(
                        400,
                        json={"errors": {"message": ['MobileNumber is not unique {"object_id"=>"2090674640783768576"}']}},
                    )
                if request.url.path.endswith("/api/contacts/2090674640783768576") and request.method == "PUT":
                    calls["update"] += 1
                    return Response(200, json={})
                return Response(200, json={})

            app.state.http_client._transport.handle_async_request = handler  # type: ignore[attr-defined,method-assign]
            assert (await client.post("/webhooks/meta/leadgen", content=body, headers={"X-Hub-Signature-256": sign(body)})).status_code == 200
            assert calls == {"create": 1, "search": settings.match_retry_attempts * 2, "update": 1}
            assert await dead_letter_errors() == []


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
                    return Response(200, json={"contact": {"id": "created-3"}})
                return Response(200, json={})

            app.state.http_client._transport.handle_async_request = handler  # type: ignore[attr-defined,method-assign]
            assert (await client.post("/webhooks/meta/leadgen", content=body, headers={"X-Hub-Signature-256": sign(body)})).status_code == 200
            assert calls["lookup"] == settings.match_retry_attempts
            assert calls["lookup"] > 1
            assert calls["create"] == 1
            assert await dead_letter_errors() == []


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
            assert (await client.post("/webhooks/meta/leadgen", content=body, headers={"X-Hub-Signature-256": sign(body)})).status_code == 200
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


# --- Campaign context ---------------------------------------------------------

CAMPAIGN_LEAD = {
    "created_time": "2026-10-09T09:00:00+0000",
    "form_id": "form-1",
    "ad_id": "ad-1",
    "ad_name": "Ad One",
    "adset_id": "adset-1",
    "adset_name": "Ad Set One",
    "campaign_id": "camp-1",
    "campaign_name": "Campaign One",
    "field_data": [
        {"name": "full_name", "values": ["Ada Lovelace"]},
        {"name": "email", "values": ["ada@example.com"]},
        {"name": "phone_number", "values": ["+919307152210"]},
    ],
}
EXPECTED_CAMPAIGN_FIELDS = {
    "cf_campaign_name": "Campaign One",
    "cf_campaign_id": "camp-1",
    "cf_ad_set": "Ad Set One",
    "cf_ad_name": "Ad One",
}


async def post_meta_lead(leadgen_id: str, handler, inspect=None) -> int:
    """POST a leadgen webhook; `inspect` (async) runs while the app and its in-memory DB are still alive."""
    body = meta_webhook_body(leadgen_id)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            app.state.http_client._transport.handle_async_request = handler  # type: ignore[attr-defined,method-assign]
            status_code = (await client.post("/webhooks/meta/leadgen", content=body, headers={"X-Hub-Signature-256": sign(body)})).status_code
            if inspect:
                await inspect()
            return status_code


@pytest.mark.asyncio
async def test_meta_lead_requests_campaign_fields_and_stamps_matched_contact() -> None:
    puts: list[dict] = []
    graph_params: list[dict] = []

    async def handler(request):
        if "graph.facebook.com" in str(request.url):
            graph_params.append(dict(request.url.params))
            return Response(200, json=CAMPAIGN_LEAD)
        if request.url.path.endswith("/api/lookup"):
            return Response(200, json={"contacts": {"records": [{"id": "c1", "email": "ada@example.com"}]}})
        if request.url.path.endswith("/api/contacts/c1") and request.method == "PUT":
            puts.append(json.loads(request.content))
            return Response(200, json={})
        return Response(200, json={})

    async def inspect() -> None:
        assert set(graph_params[0]["fields"].split(",")) >= {"field_data", "ad_name", "adset_name", "campaign_name", "campaign_id", "ad_id", "adset_id"}
        assert puts == [{"contact": {"custom_field": {**EXPECTED_CAMPAIGN_FIELDS, "cf_meta_lead_id": "l-camp"}}}]
        async with app.state.sessionmaker() as session:
            assert await Repository(session).get_leadgen_id_by_record_id("c1") == "l-camp"

    assert await post_meta_lead("l-camp", handler, inspect) == 200


@pytest.mark.asyncio
async def test_meta_lead_create_includes_campaign_fields() -> None:
    created: list[dict] = []

    async def handler(request):
        if "graph.facebook.com" in str(request.url):
            return Response(200, json=CAMPAIGN_LEAD)
        if request.url.path.endswith("/api/lookup"):
            return Response(200, json={"contacts": {"records": []}})
        if request.url.path.endswith("/api/filtered_search/contact"):
            return Response(200, json={"contacts": []})
        if request.url.path.endswith("/api/contacts") and request.method == "POST":
            created.append(json.loads(request.content))
            return Response(200, json={"contact": {"id": "new-1"}})
        return Response(200, json={})

    assert await post_meta_lead("l-camp-create", handler) == 200
    assert created[0]["contact"]["custom_field"] == {**EXPECTED_CAMPAIGN_FIELDS, "cf_meta_lead_id": "l-camp-create"}


@pytest.mark.asyncio
async def test_meta_graph_rejecting_extended_fields_falls_back_and_still_stamps_lead_id() -> None:
    puts: list[dict] = []
    graph_calls: list[dict] = []

    async def handler(request):
        if "graph.facebook.com" in str(request.url):
            graph_calls.append(dict(request.url.params))
            if "fields" in request.url.params:
                return Response(400, json={"error": {"code": 200, "message": "permission"}})
            return Response(200, json={"field_data": CAMPAIGN_LEAD["field_data"]})
        if request.url.path.endswith("/api/lookup"):
            return Response(200, json={"contacts": {"records": [{"id": "c2", "email": "ada@example.com"}]}})
        if request.url.path.endswith("/api/contacts/c2") and request.method == "PUT":
            puts.append(json.loads(request.content))
            return Response(200, json={})
        return Response(200, json={})

    async def inspect() -> None:
        assert len(graph_calls) == 2 and "fields" not in graph_calls[1]
        assert puts == [{"contact": {"custom_field": {"cf_meta_lead_id": "l-fallback"}}}]
        assert await dead_letter_errors() == []

    assert await post_meta_lead("l-fallback", handler, inspect) == 200


@pytest.mark.asyncio
async def test_meta_campaign_field_write_rejected_still_stamps_lead_id() -> None:
    puts: list[dict] = []

    async def handler(request):
        if "graph.facebook.com" in str(request.url):
            return Response(200, json=CAMPAIGN_LEAD)
        if request.url.path.endswith("/api/lookup"):
            return Response(200, json={"contacts": {"records": [{"id": "c3", "email": "ada@example.com"}]}})
        if request.url.path.endswith("/api/contacts/c3") and request.method == "PUT":
            payload = json.loads(request.content)
            puts.append(payload)
            if "cf_campaign_name" in payload["contact"]["custom_field"]:
                return Response(400, json={"errors": {"message": ["unknown custom field"]}})
            return Response(200, json={})
        return Response(200, json={})

    async def inspect() -> None:
        assert puts[-1] == {"contact": {"custom_field": {"cf_meta_lead_id": "l-badfield"}}}
        assert await dead_letter_errors() == []
        async with app.state.sessionmaker() as session:
            assert await Repository(session).get_leadgen_id_by_record_id("c3") == "l-badfield"

    assert await post_meta_lead("l-badfield", handler, inspect) == 200


@pytest.mark.asyncio
async def test_meta_graph_failure_returns_200_and_dead_letters_for_replay() -> None:
    async def handler(request):
        if "graph.facebook.com" in str(request.url):
            return Response(400, json={"error": {"code": 190, "message": "expired"}})
        return Response(200, json={})

    async def inspect() -> None:
        assert await dead_letter_errors() == ["HTTPStatusError"]
        async with app.state.sessionmaker() as session:
            result = await session.execute(select(DedupeEvent.status).where(DedupeEvent.dedupe_key == "meta_lead_capture:l-expired"))
            assert result.scalar_one() == "failed"

    assert await post_meta_lead("l-expired", handler, inspect) == 200


@pytest.mark.asyncio
async def test_meta_duplicate_webhook_delivery_processes_lead_once() -> None:
    puts: list[str] = []
    body = meta_webhook_body("l-twice")

    async def handler(request):
        if "graph.facebook.com" in str(request.url):
            return Response(200, json=CAMPAIGN_LEAD)
        if request.url.path.endswith("/api/lookup"):
            return Response(200, json={"contacts": {"records": [{"id": "c4", "email": "ada@example.com"}]}})
        if request.url.path.endswith("/api/contacts/c4") and request.method == "PUT":
            puts.append("put")
            return Response(200, json={})
        return Response(200, json={})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            app.state.http_client._transport.handle_async_request = handler  # type: ignore[attr-defined,method-assign]
            for _ in range(2):
                assert (await client.post("/webhooks/meta/leadgen", content=body, headers={"X-Hub-Signature-256": sign(body)})).status_code == 200
    assert puts == ["put"]


@pytest.mark.asyncio
async def test_failed_meta_lead_is_replayed_and_succeeds() -> None:
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import update

    from app.replay import replay_once

    state = {"graph_ok": False}
    puts: list[str] = []

    async def handler(request):
        if "graph.facebook.com" in str(request.url):
            if not state["graph_ok"]:
                return Response(400, json={"error": {"code": 190, "message": "expired"}})
            return Response(200, json=CAMPAIGN_LEAD)
        if request.url.path.endswith("/api/lookup"):
            return Response(200, json={"contacts": {"records": [{"id": "c5", "email": "ada@example.com"}]}})
        if request.url.path.endswith("/api/contacts/c5") and request.method == "PUT":
            puts.append("put")
            return Response(200, json={})
        return Response(200, json={})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            app.state.http_client._transport.handle_async_request = handler  # type: ignore[attr-defined,method-assign]
            body = meta_webhook_body("l-replay")
            assert (await client.post("/webhooks/meta/leadgen", content=body, headers={"X-Hub-Signature-256": sign(body)})).status_code == 200
            assert puts == []
            assert await replay_once(app.state, get_settings()) == 0  # too recent to replay
            old = datetime.now(timezone.utc) - timedelta(hours=1)
            async with app.state.sessionmaker() as session:
                await session.execute(update(DedupeEvent).where(DedupeEvent.dedupe_key == "meta_lead_capture:l-replay").values(updated_at=old, created_at=old))
                await session.commit()
            state["graph_ok"] = True
            assert await replay_once(app.state, get_settings()) == 1
            async with app.state.sessionmaker() as session:
                result = await session.execute(select(DedupeEvent.status).where(DedupeEvent.dedupe_key == "meta_lead_capture:l-replay"))
                assert result.scalar_one() == "success"
            assert puts == ["put"]


@pytest.mark.asyncio
async def test_reconcile_recovers_leads_the_webhook_never_delivered() -> None:
    from app.replay import reconcile_missing_leads

    puts: list[str] = []
    list_calls: list[dict] = []

    async def handler(request):
        url = str(request.url)
        path = request.url.path
        if "graph.facebook.com" in url:
            if path.endswith("/me/accounts"):
                return Response(200, json={"data": [{"id": "page-1", "access_token": "PAGE_TOKEN"}]})
            if path.endswith("/page-1/leadgen_forms"):
                assert request.url.params["access_token"] == "PAGE_TOKEN"
                return Response(200, json={"data": [{"id": "form-1"}]})
            if path.endswith("/form-1/leads"):
                list_calls.append(dict(request.url.params))
                if "after" not in request.url.params:
                    return Response(200, json={"data": [{"id": "l-known"}, {"id": "l-missed"}], "paging": {"cursors": {"after": "c1"}, "next": "x"}})
                return Response(200, json={"data": [{"id": "l-missed-2"}], "paging": {"cursors": {"after": "c2"}}})
            return Response(200, json=CAMPAIGN_LEAD)
        if path.endswith("/api/lookup"):
            return Response(200, json={"contacts": {"records": [{"id": "c9", "email": "ada@example.com"}]}})
        if path.endswith("/api/contacts/c9") and request.method == "PUT":
            puts.append(json.loads(request.content)["contact"]["custom_field"]["cf_meta_lead_id"])
            return Response(200, json={})
        return Response(200, json={})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test"):
        async with app.router.lifespan_context(app):
            app.state.http_client._transport.handle_async_request = handler  # type: ignore[attr-defined,method-assign]
            async with app.state.sessionmaker() as session:
                repo = Repository(session)
                await repo.acquire_dedupe("meta_lead_capture:l-known")
                await repo.mark_dedupe_success("meta_lead_capture:l-known")
            assert await reconcile_missing_leads(app.state, get_settings()) == 2
            assert sorted(puts) == ["l-missed", "l-missed-2"]
            assert "filtering" in list_calls[0] and "time_created" in list_calls[0]["filtering"]
            # A second pass finds nothing new.
            assert await reconcile_missing_leads(app.state, get_settings()) == 0


@pytest.mark.asyncio
async def test_reconcile_leaves_failed_leads_to_the_replay_loop() -> None:
    from app.replay import reconcile_missing_leads

    async def handler(request):
        path = request.url.path
        if path.endswith("/me/accounts"):
            return Response(200, json={"data": [{"id": "page-1", "access_token": "T"}]})
        if path.endswith("/leadgen_forms"):
            return Response(200, json={"data": [{"id": "form-1"}]})
        if path.endswith("/form-1/leads"):
            return Response(200, json={"data": [{"id": "l-failed"}]})
        return Response(200, json={})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test"):
        async with app.router.lifespan_context(app):
            app.state.http_client._transport.handle_async_request = handler  # type: ignore[attr-defined,method-assign]
            async with app.state.sessionmaker() as session:
                repo = Repository(session)
                await repo.acquire_dedupe("meta_lead_capture:l-failed")
                await repo.mark_dedupe_failed("meta_lead_capture:l-failed")
            assert await reconcile_missing_leads(app.state, get_settings()) == 0


@pytest.mark.asyncio
async def test_lead_event_for_contact_with_several_leads_uses_the_latest_leadgen_id() -> None:
    body = {"event_name": "Qualified", "contact_id": "contact-repeat"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            async with app.state.sessionmaker() as session:
                repo = Repository(session)
                await repo.save_lead_state("leadgen-first", "contact-repeat")
                await repo.save_lead_state("leadgen-second", "contact-repeat")
                assert await repo.has_record("contact-repeat", "contact")
                assert await repo.get_leadgen_id_by_record_id("contact-repeat") == "leadgen-second"

            sent: list[dict] = []

            async def handler(request):
                if "graph.facebook.com" in str(request.url) and request.method == "POST":
                    sent.append(json.loads(request.content))
                return Response(200, json={"events_received": 1})

            app.state.http_client._transport.handle_async_request = handler  # type: ignore[attr-defined,method-assign]
            response = await client.post("/webhooks/freshsales/lead-event", json=body, headers={"X-Internal-Secret": "freshsecret"})

            assert response.status_code == 200
            assert [event["user_data"] for payload in sent for event in payload["data"]] == [{"lead_id": "leadgen-second"}]
