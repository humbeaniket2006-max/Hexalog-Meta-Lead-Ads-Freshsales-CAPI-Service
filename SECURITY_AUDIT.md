# Security Audit

Audit date: 2026-08-10

## 1. Webhook Signature/Auth Verification

- Meta GET verification requires `hub.mode=subscribe`, the configured `META_VERIFY_TOKEN`, and returns only `hub.challenge`.
- Meta POST rejects missing, malformed, or invalid `X-Hub-Signature-256` with HTTP 403 before JSON validation or business logic.
- Freshsales POST rejects missing or mismatched `X-Freshsales-Signature` with HTTP 403 before payload validation or business logic.
- Meta's signature header has no timestamp. Replay of the same valid body is mitigated by DB-backed dedupe: the same `leadgen_id` cannot produce a second successful Freshsales write. Freshsales replay is similarly deduped by `record_id + status + occurred_at minute window`.

## 2. Secrets Handling

Secrets are loaded from environment variables into Pydantic `SecretStr` fields. The code uses `get_secret_value()` only when constructing outbound auth headers, Meta access-token payloads, Slack alert sends, and signature comparison. No secrets are returned in HTTP errors.

Grep command:

```bash
rg -n "(eval\\(|exec\\(|access_token|get_secret_value\\(|api_key|secret|password|token=|print\\()" app tests .env.example requirements.txt
```

Result: expected secret plumbing and test placeholders only; no hardcoded production credentials.

## 3. Injection Surfaces

DB access uses SQLAlchemy Core/ORM inserts, updates, selects, and a constant `text("SELECT 1")` health query. There is no user-controlled SQL string construction.

No `eval`, `exec`, or dynamic code execution is used.

## 4. Input Validation Gaps

Freshsales webhook payloads use strict Pydantic models with `extra="forbid"`, enums for record type/status, constrained IDs, and bounded string lengths.

Meta webhook payloads use constrained nested Pydantic models. Meta allows extra fields because webhook payloads can include provider metadata, but business logic reads only validated `field` and constrained `leadgen_id`.

No unvalidated webhook payload reaches business logic.

## 5. Idempotency/Race Conditions

Same webhook twice within 1 second:

- First request inserts a unique dedupe row with `processing`.
- Concurrent or later duplicate sees `processing` or `success` and skips side effects.
- If processing fails, the row becomes `failed`, allowing a later retry.

Two different webhooks referencing the same lead:

- Meta lead capture stores a unique `leadgen_id` in `lead_states`.
- Freshsales status webhooks must reference a known local record before a CAPI send.
- Separate statuses for the same record are allowed and deduped independently by status and timestamp window.

## 6. Error Responses

Endpoints return generic errors: `invalid payload`, `authentication failed`, `signature verification failed`, `record not recognized`, or `processing failed`. They do not expose stack traces, internal paths, payload fragments, or secret values.

## 7. Dependency Check

Initial audit found vulnerabilities in older pins:

- `python-dotenv==1.0.1`
- `pytest==8.3.4`
- `starlette==0.41.3`

Fix applied: bumped pins to `python-dotenv==1.2.2`, `pytest==9.1.1`, `pytest-asyncio==1.4.0`, and `fastapi==0.141.1`, which resolves to a fixed `starlette` release.

Final command:

```bash
python3 -m pip_audit -r requirements.txt --disable-pip --no-deps
```

Final output:

```text
No known vulnerabilities found
```

Note: `pip-audit` warned that `--no-deps` should ideally be replaced with hashed pinned dependencies. For production, generate a fully hashed lock file with `pip-compile --generate-hashes`.

## 8. Rate Limiting

Both public webhook POST endpoints call the shared in-memory limiter before body processing. Default limit is `60` requests per minute per client IP, configurable through `RATE_LIMIT_PER_MINUTE`.

For multi-instance Railway deployments, replace the in-memory limiter with Redis-backed storage so limits apply across workers.

## 9. Severity-Ranked Issues

- Critical: none found.
- High: vulnerable dependency pins found and fixed.
- Medium: Meta signature replay has no timestamp from Meta; same-body replay is deduped, but arbitrary old valid unique webhook bodies cannot be rejected by age without an upstream timestamp. No code fix applied because Meta's signature header does not provide one.
- Low: production dependency auditing should use hashed lock files instead of `--no-deps`; deferred to deployment packaging.

## Verification

```text
python3 -m pytest -q
10 passed in 2.82s
```
