# Hexalog Freshsales CAPI Service

Production FastAPI service replacing the LeadsBridge-style connector between Meta Lead Ads and Freshsales. Once verified in production, disable the native Freshsales-Meta connector so this service is the sole path for Meta leads entering Freshsales.

## Flows

- Meta Lead Ads webhook captures `leadgen_id`, fetches the full lead payload from Meta Graph API, searches Freshsales by email then phone, and stamps the configured Freshsales custom field with the Meta `leadgen_id` on the matching contact.
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

- `DATABASE_URL`: async SQLAlchemy URL, for example `sqlite+aiosqlite:///./freshsales_capi.db` or PostgreSQL via `postgresql+asyncpg://...`. Render-managed `postgres://` and `postgresql://` URLs are normalized to `postgresql+asyncpg://` at startup.
- `META_VERIFY_TOKEN`
- `META_APP_SECRET`
- `META_PAGE_ACCESS_TOKEN`
- `META_PIXEL_ID`
- `META_CAPI_ACCESS_TOKEN`
- `META_DATASET_ID`
- `FRESHSALES_DOMAIN`: full Freshsales URL including `https://`, for example `https://yourdomain.myfreshworks.com` or `https://yourdomain.myfreshworks.com/crm/sales`
- `FRESHSALES_API_KEY`
- `FRESHSALES_WEBHOOK_SECRET`
- `FRESHSALES_META_LEAD_FIELD`
- `SLACK_WEBHOOK_URL`
- `ALERT_CHANNEL`: `slack` or `none`
- `MAX_HTTP_ATTEMPTS`
- `MATCH_RETRY_ATTEMPTS`
- `MATCH_RETRY_MIN_SECONDS`
- `MATCH_RETRY_MAX_SECONDS`
- `RATE_LIMIT_PER_MINUTE`
- `DEBUG_LOG_PII`: defaults false

Use a least-privilege Freshsales token with only lead search, lead create, lead update, deal/lead read, and custom field write permissions needed by the enabled flows.

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

Current local result: `16 passed`; `No known vulnerabilities found`.
