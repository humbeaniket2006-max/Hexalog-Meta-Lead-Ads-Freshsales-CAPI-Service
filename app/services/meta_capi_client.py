import httpx

from app.config import Settings
from app.retry import retry_async


class MetaCapiClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.client = client

    async def send_event(
        self,
        leadgen_id: str,
        event_name: str,
        event_time: int,
        amount: float | None = None,
    ) -> dict[str, object]:
        event: dict[str, object] = {
            "event_name": event_name,
            "event_time": event_time,
            "action_source": "system_generated",
            "user_data": {"lead_id": leadgen_id},
        }
        if event_name == "Conversion":
            event["custom_data"] = {"value": amount or 0.0, "currency": "USD"}

        payload = {
            "data": [event],
            "access_token": self.settings.meta_capi_access_token.get_secret_value(),
        }

        async def op() -> dict[str, object]:
            response = await self.client.post(
                f"https://graph.facebook.com/v20.0/{self.settings.meta_dataset_id}/events",
                json=payload,
                timeout=self.settings.http_timeout_seconds,
            )
            response.raise_for_status()
            return response.json()

        return await retry_async(op, self.settings.max_http_attempts)
