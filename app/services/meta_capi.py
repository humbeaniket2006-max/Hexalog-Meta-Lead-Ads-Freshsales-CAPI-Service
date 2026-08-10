import time

import httpx

from app.config import Settings
from app.retry import retry_async


class MetaCapiClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.client = client

    async def send_crm_event(
        self,
        event_name: str,
        lead_id: str,
        value: float | None = None,
        currency: str | None = None,
    ) -> dict[str, object]:
        custom_data: dict[str, object] = {}
        if event_name == "Conversion":
            custom_data = {"value": value or 0.0, "currency": currency or "USD"}
        payload = {
            "data": [
                {
                    "event_name": event_name,
                    "event_time": int(time.time()),
                    "action_source": "system_generated",
                    "user_data": {"lead_id": lead_id},
                    "custom_data": custom_data,
                }
            ],
            "access_token": self.settings.meta_page_access_token.get_secret_value(),
        }

        async def op() -> dict[str, object]:
            response = await self.client.post(
                f"https://graph.facebook.com/v20.0/{self.settings.meta_pixel_id}/events",
                json=payload,
                timeout=self.settings.http_timeout_seconds,
            )
            response.raise_for_status()
            return response.json()

        return await retry_async(op, self.settings.max_http_attempts)
