import httpx

from app.config import Settings
from app.logging_config import get_logger
from app.retry import retry_async

logger = get_logger(__name__)


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
            if response.status_code >= 400:
                try:
                    error_body: object = response.json()
                except ValueError:
                    error_body = response.text
                logger.warning(
                    "meta_capi_response_error",
                    status_code=response.status_code,
                    meta_capi_error_body=error_body,
                )
            response.raise_for_status()
            return response.json()

        return await retry_async(op, self.settings.max_http_attempts)
