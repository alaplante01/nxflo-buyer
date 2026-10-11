# Changelog

## [0.5.0] - 2026-10-11 — 3.2.1 buyer compliance

Upgrade from the 3.0.0-beta.2-era buyer to AdCP 3.2.1 buyer compliance
(spec: adcontextprotocol docs @ v3.2.1 / ec882d2; schemas pinned at `/schemas/3.2.1/`).

### Accounts (3.2 account model)

- New `src/accounts/`: `list_accounts` (with pagination) and `sync_accounts`
  (provisioning mode, `idempotency_key`, explicit `billing`, sandbox / fixed-currency /
  buyer-selected-timezone handling) clients, persisted per seller in the new
  `seller_accounts` table (alembic `004`).
- Both seller models: buyer-declared (`require_operator_auth: false`, our bearer,
  natural-key `account` refs — a seller-issued `account_id` is never sent) and
  operator-authorized (`require_operator_auth: true`, operator credential from
  `NXFLO_OPERATOR_TOKEN_<SLUG>`, `{account_id}` from `list_accounts` or
  `NXFLO_OPERATOR_ACCOUNT_ID_<SLUG>`). Mode from capabilities, overridable via
  `NXFLO_ACCOUNT_MODE` / `NXFLO_ACCOUNT_MODE_<SLUG>`.
- `account` + `idempotency_key` now sent on `create_media_buy`, `update_media_buy`
  and `sync_creatives`; `idempotency_key` on `provide_performance_feedback`.
  `get_products` omits `account` until the account is provisioned and usable.
- Account status gates calls (only `active` may `create_media_buy`); `pending_approval`
  surfaces the seller's `setup.url`. Billing is never silently remapped.
- REST: `GET /accounts`, `POST /accounts/list`, `POST /accounts/sync`.

### Brand Protocol

- New `src/brand/resolver.py`: resolves `/.well-known/brand.json` (House Portfolio,
  House Redirect, authoritative_location, Brand Canonical Document with
  mutual-assertion check), cached (24 h / 1 h negative), SSRF-safe fetch.
- Builds the BrandRef (`domain`, optional `brand_id`, sorted `countries`) used in
  `get_products`, `create_media_buy` and account natural keys (replaces `brand_manifest`).
- Before `create_media_buy` / `sync_accounts`, our operator (`NXFLO_OPERATOR_DOMAIN`,
  default `nxflo.io`) must be in the brand's `authorized_operators` (brand, country,
  scope and validity window honoured). Fails closed with a coded error.

### Sales-agent authorization (adagents.json)

- New `src/authorization/adagents.py`: before `create_media_buy`, every publisher in
  `product.publisher_properties[]` must authorize the seller's exact agent URL (AdCP
  URL canonicalization) for all claimed properties (property_ids / property_tags /
  inline / publisher_properties; effective window, countries, collections, placements,
  revoked domains). Missing or invalid files fail closed. Skippable only with
  `NXFLO_SKIP_SALES_AGENT_AUTHORIZATION_CHECK=true` (sandbox use; logged).

### Buyer-side agent surface

- MCP endpoint mounted at `/mcp` (stateless streamable HTTP) answering
  `get_adcp_capabilities` with `adcp`, `supported_protocols`
  (`NXFLO_AGENT_SUPPORTED_PROTOCOLS`) and `identity.brand_json_url`; public URL is
  `NXFLO_AGENT_URL`.
- `GET /.well-known/jwks.json` publishes request-signing public keys.

### Request signing (RFC 9421, optional in 3.2)

- New `src/signing/`: AdCP 3.2 profile (`@method`, `@target-uri`, `@authority`,
  `content-type`, `content-digest`; `tag="adcp/request-signing/v1"`; padded
  sf-binary), Ed25519 and ECDSA P-256 keys from env/file, key generator
  (`python -m src.signing.keygen`), httpx2 auth hook signing `tools/call` requests the
  seller lists in `request_signing.*_for`. Behind `NXFLO_SIGNING_ENABLED` (default
  false). Verified byte-for-byte against the published 3.2 conformance vectors.

### Reliability

- Bounded retry budget per operation (<= 3 retries, <= 300 s cumulative), transient
  errors only, `retry_after` ceil/clamp, jittered backoff; mutations retried only with
  an `idempotency_key` on sellers declaring idempotency support. The session's
  context-expired retry consumes the same budget; the background poller is single-attempt.
- MCP tool errors (`isError`) are parsed into `SellerToolError` with the AdCP error
  code/recovery; structuredContent is preferred over text content.

### create_media_buy / get_products (3.2.1 shapes)

- `create_media_buy`: `idempotency_key`, `account`, `brand`, explicit
  `budget_allocation: {"mode": "fixed"}` unless a `seller_optimized` block is supplied
  (checked against `media_buy.features.seller_optimized_budget`; omitted for proposal
  execution), `format_option_refs` from `product.format_options` (deprecated
  `format_ids` only for legacy products), correlation in `context.buyer_ref`.
- `get_products`: `buying_mode: "brief"` + BrandRef; `adcp_version` on 3.2-shaped requests.

### Registry discovery

- Registry base URL is a setting: `NXFLO_REGISTRY_URL` (default
  `https://agenticadvertising.org/api/registry`) with `NXFLO_REGISTRY_FALLBACK_URL`
  (`https://adcontextprotocol.org/api/registry`). Parses the current `/agents` entry
  shape (`mcp_endpoint`, `protocol`, `member`), buys only from `type == "sales"`.
- Per-seller tokens: `NXFLO_SELLER_TOKEN_<SLUG>` now honoured for any seller.

### Housekeeping

- macOS commands in CLAUDE.md; `ruff check src/ tests/` clean under ruff 0.17
  (import sorting, narrowed/logged exception handlers, `str.removeprefix("www.")`
  in `api/publishers.py` replacing a `lstrip("www.")` character-set bug).
- Live tests read the test-agent token from `NXFLO_SELLER_TOKEN_ADCP_TEST` instead of
  hardcoding it (skipped when unset).
- Dependencies: `fastmcp>=4.1.0`, `httpx2`, `cryptography`, `idna`.

## [0.4.0] - 2026-02-10 — Production Hardening

Production readiness overhaul. Two rounds of fixes based on a full code review
that identified 38 issues (4 critical, 8 high, 12 medium, 14 low).

### Security

- **Removed hardcoded API token** from `src/config.py`. The AdCP Test Agent token
  is now read from `NXFLO_SELLER_TOKEN_ADCP_TEST` env var. Returns `None` (no auth)
  when unset. ([config.py](src/config.py))
- **Webhook secret stability**: `get_webhook_secret()` generates one ephemeral secret
  at startup instead of a new random secret per call. Prevents signature mismatch
  between registration and verification. ([webhooks/config.py](src/webhooks/config.py))
- **HMAC byte handling**: Signs over `timestamp.encode() + body` (raw bytes) instead of
  `(timestamp + body.decode()).encode()` which could alter bytes on round-trip.
  ([webhooks/receiver.py](src/webhooks/receiver.py))
- **Webhook credential redaction**: `/operations/{id}` now strips `credentials` from
  `webhook_config` before returning to API callers. ([api/routes.py](src/api/routes.py))
- **Rate limiting**: New `RateLimitMiddleware` — sliding-window per-IP rate limiter.
  Configurable via `NXFLO_RATE_LIMIT_PER_MINUTE` (default: 100). Returns 429 with
  `Retry-After` header. ([middleware.py](src/middleware.py), [config.py](src/config.py))

### Reliability

- **Circuit breaker**: New `CircuitBreaker` with three states (CLOSED/OPEN/HALF_OPEN),
  `asyncio.Lock` for safe concurrent access, and Prometheus gauge tracking.
  `CircuitBreakerRegistry` manages per-seller breakers.
  ([connections/circuit_breaker.py](src/connections/circuit_breaker.py))
- **Consistent circuit breaking**: `SellerSession._call_raw` now delegates to
  `call_seller_tool` so ALL seller calls — whether via session or direct — go through
  the circuit breaker and are instrumented with Prometheus metrics.
  ([connections/session.py](src/connections/session.py))
- **Orchestrator session consistency**: `_get_products_from_seller`, `check_delivery`,
  `poll_pending_operations`, and `list_seller_tasks` now route through `_session_call`
  instead of bypassing the session layer. Ensures context_id tracking, circuit breaking,
  and metrics on every call. ([buying/orchestrator.py](src/buying/orchestrator.py))
- **Immediate persistence on create**: `tracker.create()` is now async and calls
  `_persist()` before returning. Eliminates the crash window between operation creation
  and first persist — no more lost operations if the process dies mid-buy.
  ([buying/tracker.py](src/buying/tracker.py))
- **Poller uses fresh seller list**: `BackgroundPoller` now accepts a `Callable` instead
  of a static list. After re-discovery via `/discover`, the poller automatically sees the
  updated sellers. ([buying/poller.py](src/buying/poller.py), [main.py](src/main.py))
- **Unknown status defaults to UNKNOWN**: `update_from_response()` no longer silently
  marks unrecognized seller statuses as COMPLETED. They go to `TaskStatus.UNKNOWN`,
  preventing premature closure of active operations.
  ([buying/tracker.py](src/buying/tracker.py))
- **Reporting webhook idempotency**: `receive_reporting_webhook` now deduplicates via
  `report_id` (or payload SHA-256 hash) through the same `_check_idempotency` mechanism
  used by task webhooks. ([webhooks/receiver.py](src/webhooks/receiver.py))

### Observability

- **Structured JSON logging**: New `logging_config.py` with `CorrelationJsonFormatter`.
  Emits JSON in production (non-TTY), human-readable in dev. Per-request correlation IDs
  via `contextvars.ContextVar`. ([logging_config.py](src/logging_config.py))
- **Prometheus metrics**: HTTP request counters/histograms, seller call counters/histograms
  (with outcome labels), operation lifecycle gauges, and circuit breaker state gauges.
  Exposed at `GET /metrics`. ([metrics.py](src/metrics.py), [main.py](src/main.py))
- **Low-cardinality metric labels**: Uses `scope["route"].path` (e.g. `/operations/{operation_id}`)
  instead of raw URL path to prevent cardinality explosion from UUIDs.
  ([middleware.py](src/middleware.py))
- **Circuit breaker status in /health**: Health endpoint now includes
  `circuit_breakers.status_summary()`. ([api/routes.py](src/api/routes.py))

### Infrastructure

- **Pure ASGI middleware**: Replaced `BaseHTTPMiddleware` with raw ASGI implementation.
  Eliminates known issues with streaming responses in Starlette.
  ([middleware.py](src/middleware.py))
- **Dockerfile hardening**: Non-root user (`nxflo:1000`), correct pip install ordering
  (copy `src/__init__.py` before install), alembic files copied, healthcheck with
  60s start period. ([Dockerfile](Dockerfile))
- **Alembic migrations**: Full migration setup with async-compatible `env.py`. Initial
  migration creates 4 tables (operations, sellers, seller_capabilities, webhook_events).
  `init_db()` uses `asyncio.to_thread()` to safely run Alembic from within the running
  event loop. ([alembic/](alembic/), [models/schema.py](src/models/schema.py))
- **CI test gate**: `deploy.yml` now runs `ruff check` + `pytest tests/test_unit.py`
  before building the Docker image. Deploy job depends on test job passing.
  ([deploy.yml](.github/workflows/deploy.yml))

### Tests

- **27 unit tests** covering:
  - Circuit breaker state machine (6 tests: closed, open, half-open, probe, recovery, re-open)
  - HMAC-SHA256 + Bearer webhook verification (6 tests)
  - Webhook config credential redaction (3 tests)
  - Operation tracker lifecycle (8 tests: create, update, unknown status, error, fail, pending, buyer_ref lookup, HITL)
  - Rate limiter middleware (4 tests: under limit, over limit, separate IPs, non-HTTP passthrough)

### New Files

| File | Purpose |
|------|---------|
| `src/logging_config.py` | Structured JSON logging + correlation IDs |
| `src/metrics.py` | Prometheus metric definitions |
| `src/middleware.py` | Pure ASGI correlation ID + rate limit middleware |
| `src/connections/circuit_breaker.py` | Circuit breaker pattern for seller connections |
| `src/webhooks/config.py` | Webhook URL builder + stable secret management |
| `alembic.ini` | Alembic configuration |
| `alembic/env.py` | Async Alembic environment |
| `alembic/script.py.mako` | Migration template |
| `alembic/versions/001_initial_schema.py` | Initial database migration |
| `tests/test_unit.py` | 27 unit tests with mocked dependencies |

### Modified Files

| File | Changes |
|------|---------|
| `src/config.py` | Removed hardcoded token, added `rate_limit_per_minute` setting |
| `src/main.py` | Added rate limiter middleware, updated poller init, simplified reconciliation |
| `src/connections/seller.py` | Integrated circuit breaker + Prometheus metrics |
| `src/connections/session.py` | Delegates to `call_seller_tool` instead of own client |
| `src/buying/orchestrator.py` | All calls route through session, `create()` awaited |
| `src/buying/tracker.py` | `create()` async + immediate persist, UNKNOWN default status |
| `src/buying/poller.py` | Accepts callable for seller list |
| `src/webhooks/receiver.py` | HMAC byte fix, reporting webhook idempotency |
| `src/api/routes.py` | Credential redaction, circuit breaker in /health |
| `src/models/schema.py` | `init_db()` uses `asyncio.to_thread()` for Alembic |
| `Dockerfile` | Non-root user, install ordering fix, alembic files |
| `pyproject.toml` | Added alembic, python-json-logger, prometheus-client deps |
| `.github/workflows/deploy.yml` | Added lint+test job gating deploy |

### New Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `NXFLO_SELLER_TOKEN_ADCP_TEST` | *(none)* | Bearer token for AdCP Test Agent |
| `NXFLO_WEBHOOK_SECRET` | *(auto-generated)* | HMAC shared secret for webhooks |
| `NXFLO_RATE_LIMIT_PER_MINUTE` | `100` | API rate limit per IP per minute |
