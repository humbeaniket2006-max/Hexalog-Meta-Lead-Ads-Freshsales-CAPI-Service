import httpx

from app.config import Settings
from app.retry import retry_async


class MetaGraphClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.client = client

    async def fetch_lead(self, leadgen_id: str) -> dict[str, object]:
        async def op() -> dict[str, object]:
            response = await self.client.get(
                f"https://graph.facebook.com/v20.0/{leadgen_id}",
                params={"access_token": self.settings.meta_page_access_token.get_secret_value()},
                timeout=self.settings.http_timeout_seconds,
            )
            response.raise_for_status()
            return response.json()

        return await retry_async(op, self.settings.max_http_attempts)
