import httpx

from app.config import Settings
from app.logging_config import get_logger

logger = get_logger(__name__)


class AlertService:
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.client = client

    async def fire(self, message: str, context: dict[str, object]) -> None:
        if self.settings.alert_channel == "none" or not self.settings.slack_webhook_url:
            logger.warning("alert_suppressed", message=message, context=context)
            return
        try:
            await self.client.post(
                self.settings.slack_webhook_url.get_secret_value(),
                json={"text": message, "context": context},
                timeout=self.settings.http_timeout_seconds,
            )
        except httpx.HTTPError as exc:
            logger.error("alert_failed", error=type(exc).__name__)
