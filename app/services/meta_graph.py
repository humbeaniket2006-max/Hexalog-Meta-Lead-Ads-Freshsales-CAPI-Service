import httpx
from pydantic import ValidationError

from app.config import Settings
from app.logging_config import get_logger
from app.models.webhooks import MetaGraphLeadPayload
from app.retry import retry_async

logger = get_logger(__name__)

LEAD_FIELDS = "field_data,created_time,form_id,ad_id,ad_name,adset_id,adset_name,campaign_id,campaign_name"
RATE_LIMIT_CODES = {4, 17, 32, 613}


def graph_error_code(exc: httpx.HTTPStatusError) -> int | None:
    try:
        error = exc.response.json().get("error", {})
    except (ValueError, AttributeError):
        return None
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, int) else None


def is_rate_limited(exc: httpx.HTTPStatusError) -> bool:
    return graph_error_code(exc) in RATE_LIMIT_CODES


class MetaGraphClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.client = client

    async def fetch_lead(self, leadgen_id: str) -> MetaGraphLeadPayload:
        """Fetch a lead with campaign context; fall back to default fields if Meta rejects the extended list."""
        try:
            return await self._fetch(leadgen_id, LEAD_FIELDS)
        except httpx.HTTPStatusError as exc:
            code = graph_error_code(exc)
            # 190 = bad token: the plain fetch would fail identically, so don't mask it.
            if exc.response.status_code != 400 or code == 190:
                raise
            logger.warning("meta_graph_extended_fields_rejected", leadgen_id=leadgen_id, error_code=code)
            return await self._fetch(leadgen_id, None)

    async def _fetch(self, leadgen_id: str, fields: str | None) -> MetaGraphLeadPayload:
        params = {"access_token": self.settings.meta_page_access_token.get_secret_value()}
        if fields:
            params["fields"] = fields

        async def op() -> MetaGraphLeadPayload:
            response = await self.client.get(
                f"https://graph.facebook.com/{self.settings.meta_graph_version}/{leadgen_id}",
                params=params,
                timeout=self.settings.http_timeout_seconds,
            )
            response.raise_for_status()
            try:
                return MetaGraphLeadPayload.model_validate(response.json())
            except ValidationError as exc:
                raise ValueError("invalid Meta lead payload") from exc

        return await retry_async(op, self.settings.max_http_attempts, is_retryable=is_rate_limited)
