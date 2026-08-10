import httpx
from pydantic import ValidationError

from app.config import Settings
from app.models.webhooks import MetaGraphLeadPayload
from app.retry import retry_async


class MetaGraphClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.client = client

    async def fetch_lead(self, leadgen_id: str) -> MetaGraphLeadPayload:
        async def op() -> MetaGraphLeadPayload:
            response = await self.client.get(
                f"https://graph.facebook.com/v20.0/{leadgen_id}",
                params={"access_token": self.settings.meta_page_access_token.get_secret_value()},
                timeout=self.settings.http_timeout_seconds,
            )
            response.raise_for_status()
            try:
                return MetaGraphLeadPayload.model_validate(response.json())
            except ValidationError as exc:
                raise ValueError("invalid Meta lead payload") from exc

        return await retry_async(op, self.settings.max_http_attempts)
