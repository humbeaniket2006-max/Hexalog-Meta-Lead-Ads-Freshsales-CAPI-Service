# Hexalog Freshsales CAPI Service

Production FastAPI service replacing the LeadsBridge-style connector between Meta Lead Ads and Freshsales. Once verified in production, disable the native Freshsales-Meta connector so this service is the sole path for Meta leads entering Freshsales.

## Flows

- Meta Lead Ads webhook captures `leadgen_id` and returns 200 immediately. A background task fetches the lead (with campaign context) from the Meta Graph API, searches Freshsales by email then phone, and stamps the Meta `leadgen_id` plus campaign fields on the matching contact. If no contact exists after the match retries, the service creates it. Leads that fail are retried by a replay loop (see Campaign context).
- Freshsales status webhook validates a shared secret, confirms the record exists in local state, fetches the full Freshsales record, maps the status to a Meta CRM CAPI event, and sends it with `lead_id`.
- Freshsales lead-event webhook validates `X-Internal-Secret`, resolves contact events through local lead state, and sends Qualified, NotQualified, or Conversion events to Meta CAPI.

Meta lead field mapping is configured in `app/config.py` through `meta_to_freshsales_field_mapping`. The default maps `full_name`, `email`, `phone_number`, and `phone`; extend that mapping when Instant Forms add new custom questions.

Status mapping:

- `CC Qualified` -> `Qualified`
- `Future Prospect` -> `Qualified`
- `Dropped` -> `Not Qualified`
- `Won` -> `Conversion`

## Local Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload --port 8000
```

For Railway, the included `Procfile` respects `$PORT`.

## Environment

All secrets come from environment variables. Do not commit `.env`.

- `DATABASE_URL`: defaults to `sqlite+aiosqlite:///./freshsales_capi.db` when unset. PostgreSQL URLs using `postgres://` or `postgresql://` are normalized to `postgresql+asyncpg://` at startup. On Supabase free tier, use the Session Pooler connection string, for example `postgresql://postgres.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres`; do not use the Direct connection or Transaction pooler. The Direct connection is IPv6-only on Supabase free tier, while Render outbound networking needs the IPv4-compatible Session Pooler on port 5432. Supabase SSL query params such as `sslmode=require` are stripped in `app/db/session.py` and passed to asyncpg via `connect_args`.
- `META_VERIFY_TOKEN`
- `META_APP_SECRET`
- `META_PAGE_ACCESS_TOKEN`
- `META_PIXEL_ID`
- `META_CAPI_ACCESS_TOKEN`
- `META_DATASET_ID`
- `FRESHSALES_DOMAIN`: full Freshsales URL including `https://`, for example `https://yourdomain.myfreshworks.com` or `https://yourdomain.myfreshworks.com/crm/sales`
- `FRESHSALES_API_KEY`
- `FRESHSALES_WEBHOOK_SECRET`
- `FRESHSALES_META_LEAD_FIELD`: internal name of the Meta lead id field, default `cf_meta_lead_id`
- `FRESHSALES_CAMPAIGN_NAME_FIELD`, `FRESHSALES_CAMPAIGN_ID_FIELD`, `FRESHSALES_AD_SET_FIELD`, `FRESHSALES_AD_NAME_FIELD`: internal names of the campaign fields, defaults `cf_campaign_name`, `cf_campaign_id`, `cf_ad_set`, `cf_ad_name`
- `META_GRAPH_VERSION`: Graph API version for lead fetches and CAPI events, default `v20.0`. Check the [changelog](https://developers.facebook.com/docs/graph-api/changelog) and bump it deliberately.
- `META_PAGE_ID`: optional; needed only if the token can access more than one Page
- `LEAD_RECONCILE_INTERVAL_SECONDS` (default 900, `0` disables), `LEAD_RECONCILE_LOOKBACK_HOURS` (default 48), `LEAD_RECONCILE_STARTUP_DELAY_SECONDS` (default 15)
- `LEAD_REPLAY_INTERVAL_SECONDS` (default 300, `0` disables), `LEAD_REPLAY_MAX_AGE_HOURS` (default 48), `LEAD_MAX_CONCURRENCY` (default 10)
- `SLACK_WEBHOOK_URL`
- `ALERT_CHANNEL`: `slack` or `none`
- `MAX_HTTP_ATTEMPTS`
- `MATCH_RETRY_ATTEMPTS`
- `MATCH_RETRY_MIN_SECONDS`
- `MATCH_RETRY_MAX_SECONDS`
- `RATE_LIMIT_PER_MINUTE`
- `DEBUG_LOG_PII`: defaults false

Use a least-privilege Freshsales token with only lead search, lead create, lead update, deal/lead read, and custom field write permissions needed by the enabled flows.

## Campaign context

Every Meta lead stamped into Freshsales carries `campaign_name`, `campaign_id`, `adset_name`, `ad_name` and the Meta lead id.

**Required Freshsales contact custom fields** (single-line text; Admin Settings → Field Settings → Contacts). Internal names must match the env vars above:

| Label | Internal name |
|---|---|
| Campaign Name | `cf_campaign_name` |
| Campaign ID | `cf_campaign_id` |
| Ad Set | `cf_ad_set` |
| Ad Name | `cf_ad_name` |
| Meta Lead ID | `cf_meta_lead_id` |

**Behaviour**
- The webhook acknowledges immediately; processing runs in a background task (bounded by `LEAD_MAX_CONCURRENCY`).
- Duplicate deliveries are skipped through the `dedupe_events` table keyed by `leadgen_id`.
- If Meta rejects the extended field list, the lead is fetched with default fields and still stamped with its lead id (`campaign_fields_missing` is logged). If Freshsales rejects the campaign fields (for example a field does not exist), the lead id is still written (`campaign_fields_write_failed`).
- Failed leads are recorded in `dead_letters` and marked `failed`. A replay loop in the app retries failed or abandoned leads, backing off as they age, until `LEAD_REPLAY_MAX_AGE_HOURS`. Meta no longer retries these itself because the webhook returns 200.
- Meta rate-limit errors (codes 4, 17, 32, 613) are retried with backoff. Token or permission errors (190, 200, 10) fail the lead into the replay loop and are logged.
- Logs contain `leadgen_id`, `campaign_id` and the Freshsales record id, never tokens or raw contact details. `httpx` request logging is set to WARNING because Graph URLs carry the access token.

**Required Meta permissions** (verify against the [Lead Retrieval docs](https://developers.facebook.com/docs/marketing-api/guides/lead-ads/retrieving)): `leads_retrieval`, `pages_show_list`, `pages_read_engagement`, `pages_manage_metadata` (to subscribe the Page to `leadgen`), and `ads_read` or `ads_management` for campaign, ad set and ad names. Run the read-only diagnostic to check what the deployed token actually has:

```bash
python3 -m scripts.check_meta_setup --leadgen-id <real leadgen id> --page-id <page id>
```

The Page-level `leadgen` webhook subscription is set up manually in Meta, not by this service.

## Keeping the free tier awake, and catching up after downtime

Render's free tier sleeps after about 15 minutes without traffic, and a request to a sleeping instance can time out on Meta's side. Two layers cover this:

**1. Wake it up.** A sleeping service cannot wake itself, so an external pinger is needed. `.github/workflows/keep-alive.yml` pings `/health` every 5 minutes with patient retries, but GitHub's scheduler is best-effort and can skip runs. Add an independent monitor as a second pinger (for example UptimeRobot or cron-job.org, whose free plans typically allow a 5-minute interval; check their current limits), pointed at `https://<your-service>.onrender.com/health`. A monitor that alerts you when the service is down also tells you early.

**2. Catch up after waking.** A reconcile job runs about 15 seconds after every startup (so right after a wake-up), then every `LEAD_RECONCILE_INTERVAL_SECONDS`. It lists the Page's lead forms, asks Meta for each form's leads from the last `LEAD_RECONCILE_LOOKBACK_HOURS`, and processes any lead id the service has never seen: the same flow as the webhook, so each recovered lead gets its Meta lead id and campaign, ad set and ad fields. Leads that failed earlier are left to the replay loop. If the service was down for longer than the lookback window, raise `LEAD_RECONCILE_LOOKBACK_HOURS` (up to 168) and restart; the job is safe to rerun because of dedupe. Look for `meta_lead_reconciled` and `meta_lead_reconcile_done` in the logs.

The reconcile job needs a token that can read the Page's leads. With a system user token the Page is discovered through `me/accounts`; set `META_PAGE_ID` if the token can reach several Pages.

## Security Notes

- Meta setup verification uses the `hub.challenge` handshake.
- Meta POSTs require `X-Hub-Signature-256`.
- Freshsales POSTs require `X-Freshsales-Signature` matching `FRESHSALES_WEBHOOK_SECRET`.
- Public webhook endpoints are rate-limited.
- INFO logs use hashed PII references only.
- Run `python3 -m pip_audit -r requirements.txt --disable-pip --no-deps` before every deploy. Prefer a hashed lock file for production.

## Verification

```bash
python3 -m pytest -q
python3 -m pip_audit -r requirements.txt --disable-pip --no-deps
```

Current local result: `36 passed`.
