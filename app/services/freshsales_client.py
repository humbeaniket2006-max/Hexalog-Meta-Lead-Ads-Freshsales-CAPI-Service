import re
import sys

import httpx

from app.config import Settings
from app.retry import retry_async


class DuplicateContactError(RuntimeError):
    def __init__(self, object_id: str) -> None:
        super().__init__(f"Freshsales contact already exists: {object_id}")
        self.object_id = object_id


class FreshsalesClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.client = client

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Token token={self.settings.freshsales_api_key.get_secret_value()}"}

    @property
    def base_url(self) -> str:
        return str(self.settings.freshsales_domain).rstrip("/")

    async def search_contacts_by_field(self, field_name: str, value: str) -> list[dict[str, object]]:
        params = {field_name: value}

        async def op() -> list[dict[str, object]]:
            response = await self.client.get(
                f"{self.base_url}/api/contacts/search",
                headers=self.headers,
                params=params,
                timeout=self.settings.http_timeout_seconds,
            )
            if response.status_code == 404:
                return []
            response.raise_for_status()
            data = response.json()
            contacts = data.get("contacts", []) if isinstance(data, dict) else []
            return [contact for contact in contacts if isinstance(contact, dict)]

        return await retry_async(op, self.settings.max_http_attempts)

    async def find_contact_by_email_or_phone(self, email: str | None, phone: str | None) -> dict[str, object] | None:
        if email:
            email_matches = await self.search_contacts_by_field("email", email)
            if len(email_matches) == 1:
                return email_matches[0]
        if phone:
            phone_matches = await self.search_contacts_by_field("mobile_number", phone)
            if len(phone_matches) == 1:
                return phone_matches[0]
        return None

    async def create_contact(self, contact_fields: dict[str, object], leadgen_id: str) -> dict[str, object]:
        payload = {"contact": self._with_meta_lead_id(contact_fields, leadgen_id)}

        async def op() -> dict[str, object]:
            response = await self.client.post(
                f"{self.base_url}/api/contacts",
                headers=self.headers,
                json=payload,
                timeout=self.settings.http_timeout_seconds,
            )
            duplicate_object_id = self._duplicate_mobile_contact_id(response)
            if duplicate_object_id:
                raise DuplicateContactError(duplicate_object_id)
            if response.status_code >= 400:
                print(
                    f"FRESHSALES_CREATE_CONTACT_ERROR status={response.status_code} "
                    f"body={response.text} payload={payload}",
                    file=sys.stderr,
                    flush=True,
                )
            response.raise_for_status()
            data = response.json()
            contact = data.get("contact", data) if isinstance(data, dict) else {}
            return contact if isinstance(contact, dict) else {}

        return await retry_async(op, self.settings.max_http_attempts)

    async def update_contact(self, record_id: str, contact_fields: dict[str, object], leadgen_id: str) -> None:
        payload = {"contact": self._with_meta_lead_id(contact_fields, leadgen_id)}

        async def op() -> None:
            response = await self.client.put(
                f"{self.base_url}/api/contacts/{record_id}",
                headers=self.headers,
                json=payload,
                timeout=self.settings.http_timeout_seconds,
            )
            if response.status_code >= 400:
                print(
                    f"FRESHSALES_UPDATE_CONTACT_ERROR status={response.status_code} "
                    f"body={response.text} payload={payload}",
                    file=sys.stderr,
                    flush=True,
                )
            response.raise_for_status()

        await retry_async(op, self.settings.max_http_attempts)

    async def update_meta_lead_id(self, record_id: str, leadgen_id: str) -> None:
        payload = {"contact": {"custom_field": {self.settings.freshsales_meta_lead_field: leadgen_id}}}

        async def op() -> None:
            response = await self.client.put(
                f"{self.base_url}/api/contacts/{record_id}",
                headers=self.headers,
                json=payload,
                timeout=self.settings.http_timeout_seconds,
            )
            if response.status_code >= 400:
                print(
                    f"FRESHSALES_UPDATE_META_LEAD_ID_ERROR status={response.status_code} "
                    f"body={response.text} payload={payload}",
                    file=sys.stderr,
                    flush=True,
                )
            response.raise_for_status()

        await retry_async(op, self.settings.max_http_attempts)

    def _with_meta_lead_id(self, lead_fields: dict[str, object], leadgen_id: str) -> dict[str, object]:
        payload = dict(lead_fields)
        custom_field = payload.get("custom_field")
        if not isinstance(custom_field, dict):
            custom_field = {}
        custom_field = dict(custom_field)
        custom_field[self.settings.freshsales_meta_lead_field] = leadgen_id
        payload["custom_field"] = custom_field
        return payload

    def _duplicate_mobile_contact_id(self, response: httpx.Response) -> str | None:
        if response.status_code != 400:
            return None
        try:
            data = response.json()
        except ValueError:
            return None
        errors = data.get("errors") if isinstance(data, dict) else None
        if not isinstance(errors, dict):
            return None
        messages = errors.get("message")
        if not isinstance(messages, list):
            return None
        for message in messages:
            if not isinstance(message, str):
                continue
            if "MobileNumber is not unique" not in message:
                continue
            match = re.search(r'"object_id"=>"(\d+)"', message)
            if match:
                return match.group(1)
        return None

    async def get_record(self, record_type: str, record_id: str) -> dict[str, object]:
        endpoint = "contacts" if record_type == "contact" else "deals"

        async def op() -> dict[str, object]:
            response = await self.client.get(
                f"{self.base_url}/api/{endpoint}/{record_id}",
                headers=self.headers,
                timeout=self.settings.http_timeout_seconds,
            )
            response.raise_for_status()
            return response.json()

        return await retry_async(op, self.settings.max_http_attempts)
