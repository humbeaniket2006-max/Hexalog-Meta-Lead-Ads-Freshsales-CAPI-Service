import httpx

from app.config import Settings
from app.retry import retry_async


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

    async def find_lead_by_contact(self, email: str | None, phone: str | None) -> dict[str, object] | None:
        params = {k: v for k, v in {"email": email, "phone": phone}.items() if v}

        async def op() -> dict[str, object] | None:
            response = await self.client.get(
                f"{self.base_url}/api/leads/search",
                headers=self.headers,
                params=params,
                timeout=self.settings.http_timeout_seconds,
            )
            if response.status_code == 404:
                return None
            response.raise_for_status()
            data = response.json()
            leads = data.get("leads", []) if isinstance(data, dict) else []
            return leads[0] if leads else None

        return await retry_async(op, self.settings.max_http_attempts)

    async def update_meta_lead_id(self, record_id: str, leadgen_id: str) -> None:
        payload = {"lead": {"custom_field": {self.settings.freshsales_meta_lead_field: leadgen_id}}}

        async def op() -> None:
            response = await self.client.put(
                f"{self.base_url}/api/leads/{record_id}",
                headers=self.headers,
                json=payload,
                timeout=self.settings.http_timeout_seconds,
            )
            response.raise_for_status()

        await retry_async(op, self.settings.max_http_attempts)

    async def get_record(self, record_type: str, record_id: str) -> dict[str, object]:
        endpoint = "leads" if record_type == "lead" else "deals"

        async def op() -> dict[str, object]:
            response = await self.client.get(
                f"{self.base_url}/api/{endpoint}/{record_id}",
                headers=self.headers,
                timeout=self.settings.http_timeout_seconds,
            )
            response.raise_for_status()
            return response.json()

        return await retry_async(op, self.settings.max_http_attempts)
