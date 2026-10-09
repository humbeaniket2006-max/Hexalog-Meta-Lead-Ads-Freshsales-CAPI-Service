"""Read-only diagnostic for the Meta side of the lead-capture setup.

Makes GET requests only. Never writes to Meta and never prints tokens.

Usage:
    python -m scripts.check_meta_setup [--page-id PAGE_ID] [--leadgen-id LEADGEN_ID]

Env (read from the process environment or .env):
    META_PAGE_ACCESS_TOKEN  required
    META_APP_SECRET         optional, enables appsecret_proof on every call
    META_APP_ID             optional, uses an app token for /debug_token
    META_PAGE_ID            optional, same as --page-id
    META_GRAPH_VERSION      optional, default v20.0
"""

import argparse
import hashlib
import hmac
import os
import sys
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv

REQUIRED_SCOPES = {
    "leads_retrieval": "needed to read lead data (and Advanced Access if leads come from non-role users)",
    "pages_show_list": "needed to list and access the Page",
    "pages_read_engagement": "needed to read Page-owned data such as leads",
    "pages_manage_metadata": "needed to subscribe the app to the Page webhook (leadgen)",
}
OPTIONAL_SCOPES = {
    "ads_read": "may be needed for ad_name / adset_name / campaign_name; 'ads_management' also works",
    "ads_management": "alternative to ads_read for campaign/ad names",
}
LEAD_FIELDS = "field_data,created_time,form_id,ad_id,ad_name,adset_id,adset_name,campaign_id,campaign_name"
CONTEXT_FIELDS = ["created_time", "form_id", "ad_id", "ad_name", "adset_id", "adset_name", "campaign_id", "campaign_name"]
ERROR_HINTS = {
    190: "Token expired/invalidated. Generate a new Page or system user token and update the env var.",
    200: "Permission missing. Check leads_retrieval / pages_* scopes and Lead Access in Business Settings.",
    10: "Permission denied. The token owner lacks the Page task or the app lacks the permission.",
    17: "User-level rate limit. Slow down and retry later.",
    4: "App-level rate limit. Slow down and retry later.",
    32: "Page-level rate limit. Slow down and retry later.",
    613: "Call-rate limit exceeded. Slow down and retry later.",
    100: "Invalid parameter/unknown object. The lead may be deleted or on a Page the token cannot read.",
}

results: list[tuple[str, str, str]] = []


def record(name: str, ok: bool | None, detail: str = "", fix: str = "") -> None:
    status = "PASS" if ok else ("WARN" if ok is None else "FAIL")
    line = f"[{status}] {name}"
    if detail:
        line += f" - {detail}"
    print(line)
    if status != "PASS" and fix:
        print(f"       fix: {fix}")
    results.append((status, name, detail))


class Graph:
    def __init__(self, client: httpx.Client, token: str, app_secret: str | None, version: str) -> None:
        self.client = client
        self.token = token
        self.app_secret = app_secret
        self.base = f"https://graph.facebook.com/{version}"

    def get(self, path: str, token: str | None = None, **params: str) -> tuple[int, dict]:
        token = token or self.token
        params["access_token"] = token
        if self.app_secret:
            params["appsecret_proof"] = hmac.new(self.app_secret.encode(), token.encode(), hashlib.sha256).hexdigest()
        try:
            response = self.client.get(f"{self.base}{path}", params=params, timeout=20)
        except httpx.HTTPError as exc:
            return 0, {"error": {"message": f"network error: {type(exc).__name__}", "code": 0}}
        try:
            body = response.json()
        except ValueError:
            body = {"error": {"message": "non-JSON response", "code": 0}}
        return response.status_code, body


def describe_error(body: dict) -> tuple[str, str]:
    error = body.get("error", {}) if isinstance(body, dict) else {}
    code = error.get("code")
    message = str(error.get("message", "unknown error"))[:200]
    sub = error.get("error_subcode")
    label = f"code {code}" + (f"/{sub}" if sub else "") + f": {message}"
    return label, ERROR_HINTS.get(code, "See https://developers.facebook.com/docs/graph-api/guides/error-handling")


def check_debug_token(graph: Graph, app_id: str | None, app_secret: str | None) -> dict:
    inspector = f"{app_id}|{app_secret}" if app_id and app_secret else graph.token
    status, body = graph.get("/debug_token", token=inspector, input_token=graph.token)
    if status != 200:
        detail, fix = describe_error(body)
        record("debug_token", False, detail, fix)
        return {}
    data = body.get("data", {})
    record("token is valid", bool(data.get("is_valid")), f"type={data.get('type')}", "Generate a new token; this one is not valid.")
    expires = data.get("expires_at", 0)
    if not expires:
        record("token expiry", True, "never expires")
    else:
        when = datetime.fromtimestamp(expires, tz=timezone.utc)
        days = (when - datetime.now(timezone.utc)).days
        record(
            "token expiry",
            days > 14,
            f"expires {when:%Y-%m-%d} ({days} days)",
            "Replace with a system user token (non-expiring) or a long-lived Page token before this date.",
        )
    data_expiry = data.get("data_access_expires_at", 0)
    if data_expiry:
        when = datetime.fromtimestamp(data_expiry, tz=timezone.utc)
        record("data access expiry", (when - datetime.now(timezone.utc)).days > 14, f"{when:%Y-%m-%d}", "User must re-authorize the app.")
    granted = set(data.get("scopes", []))
    for scope, why in REQUIRED_SCOPES.items():
        record(f"scope {scope}", scope in granted, "" if scope in granted else why, f"Add '{scope}' and re-issue the token.")
    if not (granted & set(OPTIONAL_SCOPES)):
        record("scope ads_read/ads_management", None, "neither granted; campaign/ad/adset names may come back empty", "Add ads_read if names are empty in the lead test below.")
    else:
        record("scope ads_read/ads_management", True)
    return data


def resolve_page_id(graph: Graph, page_id: str | None) -> str | None:
    if page_id:
        return page_id
    status, body = graph.get("/me", fields="id,name")
    if status == 200 and body.get("id"):
        return str(body["id"])
    return None


def check_subscription(graph: Graph, page_id: str, app_id: str | None) -> None:
    status, body = graph.get(f"/{page_id}/subscribed_apps")
    if status != 200:
        detail, fix = describe_error(body)
        record("page subscribed_apps", False, detail, fix)
        return
    apps = body.get("data", [])
    if not apps:
        record("leadgen subscription", False, "no apps subscribed to this Page", "POST /{page-id}/subscribed_apps with subscribed_fields=leadgen (needs pages_manage_metadata).")
        return
    with_leadgen = [a for a in apps if "leadgen" in (a.get("subscribed_fields") or [])]
    record("leadgen subscription", bool(with_leadgen), f"{len(with_leadgen)} of {len(apps)} subscribed app(s) have leadgen", "Subscribe the app to the 'leadgen' field on the Page.")
    if app_id:
        ours = any(str(a.get("id")) == str(app_id) for a in with_leadgen)
        record("this app is subscribed to leadgen", ours, "", "Subscribe this app (not only another app) to the Page.")
    if len(with_leadgen) > 1:
        names = ", ".join(str(a.get("name") or a.get("id")) for a in with_leadgen)
        record("other apps on leadgen", None, f"multiple apps receive leads: {names}", "Confirm only intended CRMs/apps are subscribed to avoid duplicate processing.")


def check_lead(graph: Graph, leadgen_id: str) -> None:
    status, body = graph.get(f"/{leadgen_id}", fields=LEAD_FIELDS)
    if status != 200:
        detail, fix = describe_error(body)
        record(f"fetch lead {leadgen_id}", False, detail, fix)
        return
    record(f"fetch lead {leadgen_id}", True)
    for name in CONTEXT_FIELDS:
        value = body.get(name)
        record(f"lead field {name}", bool(value), "populated" if value else "empty/missing", "Campaign/ad names need ads_read (or ads_management) and ad account access for the token owner; test leads from the Testing Tool have no ad context.")
    field_data = body.get("field_data", [])
    names = sorted(str(f.get("name")) for f in field_data if isinstance(f, dict))
    record("lead field_data", bool(field_data), f"{len(names)} form fields: {', '.join(names)}")


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--page-id", default=os.environ.get("META_PAGE_ID"))
    parser.add_argument("--leadgen-id")
    args = parser.parse_args()

    token = os.environ.get("META_PAGE_ACCESS_TOKEN", "").strip()
    if not token:
        record("META_PAGE_ACCESS_TOKEN set", False, "", "Set META_PAGE_ACCESS_TOKEN in the environment or .env.")
        return 1
    app_secret = os.environ.get("META_APP_SECRET") or None
    app_id = os.environ.get("META_APP_ID") or None
    version = os.environ.get("META_GRAPH_VERSION", "v20.0")
    print(f"Graph API version: {version} (verify it is still supported: https://developers.facebook.com/docs/graph-api/changelog)")
    print(f"appsecret_proof: {'enabled' if app_secret else 'disabled (META_APP_SECRET not set)'}")

    with httpx.Client() as client:
        graph = Graph(client, token, app_secret, version)
        data = check_debug_token(graph, app_id, app_secret)
        page_id = resolve_page_id(graph, args.page_id)
        if page_id:
            check_subscription(graph, page_id, app_id or data.get("app_id"))
        else:
            record("page id", False, "could not resolve", "Pass --page-id or set META_PAGE_ID (the token may not be a Page token).")
        if args.leadgen_id:
            check_lead(graph, args.leadgen_id)
        else:
            print("(skipped lead fetch: pass --leadgen-id to test the campaign fields)")

    failed = [r for r in results if r[0] == "FAIL"]
    print(f"\n{len(results) - len(failed)} ok/warn, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
