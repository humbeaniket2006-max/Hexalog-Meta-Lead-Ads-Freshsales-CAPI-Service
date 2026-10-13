import json

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

    async def _get_json(self, path: str, params: dict[str, str], token: str | None = None) -> dict[str, object]:
        params = {**params, "access_token": token or self.settings.meta_page_access_token.get_secret_value()}

        async def op() -> dict[str, object]:
            response = await self.client.get(
                f"https://graph.facebook.com/{self.settings.meta_graph_version}/{path}",
                params=params,
                timeout=self.settings.http_timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
            return data if isinstance(data, dict) else {}

        return await retry_async(op, self.settings.max_http_attempts, is_retryable=is_rate_limited)

    async def _paged_ids(
        self, path: str, params: dict[str, str], token: str | None = None, max_pages: int = 20
    ) -> list[str]:
        ids: list[str] = []
        after: str | None = None
        for _ in range(max_pages):
            page_params = {**params, **({"after": after} if after else {})}
            data = await self._get_json(path, page_params, token)
            items = data.get("data")
            for item in items if isinstance(items, list) else []:
                if isinstance(item, dict) and isinstance(item.get("id"), str):
                    ids.append(item["id"])
            paging = data.get("paging")
            cursors = paging.get("cursors") if isinstance(paging, dict) else None
            has_next = isinstance(paging, dict) and bool(paging.get("next"))
            after = cursors.get("after") if has_next and isinstance(cursors, dict) else None
            if not after:
                break
        return ids

    async def resolve_page(self) -> tuple[str, str | None]:
        """Return (page_id, page_access_token). System user tokens need me/accounts; Page tokens answer /me."""
        wanted = self.settings.meta_page_id
        try:
            data = await self._get_json("me/accounts", {"fields": "id,access_token", "limit": "100"})
            pages = [p for p in data.get("data", []) if isinstance(p, dict) and isinstance(p.get("id"), str)]  # type: ignore[union-attr]
        except httpx.HTTPStatusError:
            pages = []
        if wanted:
            for page in pages:
                if page["id"] == wanted:
                    return wanted, page.get("access_token")
            return wanted, None
        if len(pages) == 1:
            return pages[0]["id"], pages[0].get("access_token")
        if pages:
            raise ValueError("token can access several Pages; set META_PAGE_ID")
        data = await self._get_json("me", {"fields": "id"})
        page_id = data.get("id")
        if not isinstance(page_id, str) or not page_id:
            raise ValueError("could not resolve Meta page id; set META_PAGE_ID")
        return page_id, None

    async def list_form_ids(self, page_id: str, page_token: str | None = None) -> list[str]:
        return await self._paged_ids(f"{page_id}/leadgen_forms", {"fields": "id", "limit": "100"}, page_token)

    async def list_lead_ids_since(self, form_id: str, since_unix: int) -> list[str]:
        filtering = json.dumps([{"field": "time_created", "operator": "GREATER_THAN", "value": since_unix}])
        return await self._paged_ids(f"{form_id}/leads", {"fields": "id", "limit": "100", "filtering": filtering})
