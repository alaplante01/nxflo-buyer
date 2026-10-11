# CLAUDE.md

## Project Overview

Nexflo Buyer — An AdCP buying agent that discovers seller agents on the AdCP registry and purchases advertising inventory on behalf of advertisers. This is the **demand side** of the AdCP (Ad Context Protocol) ecosystem. Targets **AdCP 3.2.1** buyer compliance (accounts, Brand Protocol, adagents.json authorization, optional RFC 9421 request signing).

## Architecture

```
Advertiser/AI Client
       |
  Nexflo Buyer (this repo)
       |
       +-- calls seller agents via MCP (Model Context Protocol)
       +-- discovers sellers from the AdCP registry (primary + fallback)
       +-- verifies brand.json authorized_operators before buying for a brand
       +-- verifies the seller in each publisher's adagents.json before create_media_buy
       +-- provisions / resolves accounts (list_accounts, sync_accounts)
       +-- tracks async operations (submitted -> completed)
       +-- exposes REST API for campaign management
       +-- serves its own MCP endpoint (get_adcp_capabilities) + JWKS for signature verifiers
```

## Key Modules

- `src/discovery/registry.py` — Fetches sellers from the AdCP registry (`{NXFLO_REGISTRY_URL}/agents`, fallback `NXFLO_REGISTRY_FALLBACK_URL`; only `type == "sales"`) + local config; probes capabilities; sets per-seller account mode / operator credential
- `src/connections/seller.py` — MCP client wrapper (FastMCP StreamableHttpTransport); credential per account mode; optional request signing; bounded retry budget
- `src/connections/retry.py` — Retry budget (<= 3 retries, <= 300 s per operation) and AdCP error recovery classification
- `src/accounts/` — AdCP 3.2 accounts: `list_accounts` / `sync_accounts`, account persistence, AccountRef resolution (natural key vs `account_id`)
- `src/brand/resolver.py` — brand.json resolution (cached), BrandRef builder, `authorized_operators` check (fail closed)
- `src/authorization/adagents.py` — Sales-agent authorization against publisher `adagents.json` (fail closed)
- `src/signing/` — RFC 9421 request signing (AdCP profile), key loading, JWKS, `python -m src.signing.keygen`
- `src/protocol/` — AdCP URL canonicalization, SSRF-aware JSON fetcher for trust-root documents
- `src/agent_server/mcp.py` — This buyer's MCP server answering `get_adcp_capabilities`
- `src/buying/orchestrator.py` — Core workflow: discover → get_products → rank → (brand + adagents + account checks) → create_media_buy → monitor
- `src/buying/requests.py` — Pure AdCP 3.2.1 request builders (create_media_buy, get_products, format selectors, budget_allocation)
- `src/buying/tracker.py` — Async task lifecycle state machine
- `src/api/routes.py` — FastAPI REST endpoints
- `src/main.py` — Entry point with lifespan management; mounts the MCP endpoint and JWKS route

## Commands (macOS)

```bash
# Setup
uv venv --python 3.11 .venv
uv pip install -e ".[dev]"

# Run
.venv/bin/python -m src.main

# Unit tests (mocked HTTP, no network except the two live registry tests)
.venv/bin/python -m pytest -q

# Live read-only tests against the AdCP test agent (token = public shared test token from the AdCP docs)
NXFLO_SELLER_TOKEN_ADCP_TEST=... .venv/bin/python -m pytest tests/test_live_agent.py -v -s

# Lint
.venv/bin/ruff check src/ tests/

# Generate a request-signing key (private key written 0600, never overwritten; public JWKS printed)
.venv/bin/python -m src.signing.keygen --kid nxflo-buyer-2026-10 --private-out ~/.nxflo/signing-key.pem
```

Note: app startup probes every configured + registry seller (connect, list_tools, get_adcp_capabilities).

## API Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | /discover | Discover seller agents |
| POST | /products | Search products across sellers (`brand_domain`, `brand_id`, `brand_countries` optional) |
| POST | /buy | Full buy workflow (discover → rank → brand/adagents/account checks → purchase) |
| GET | /operations | List tracked operations |
| POST | /operations/poll | Poll pending operations |
| POST | /media-buy/update | update_media_buy (account + idempotency_key added automatically) |
| POST | /media-buy/creatives/sync | sync_creatives (account + idempotency_key added automatically) |
| GET | /accounts | Accounts persisted from list/sync calls (`?seller_url=`) |
| POST | /accounts/list | Call `list_accounts` on a seller |
| POST | /accounts/sync | Call `sync_accounts` (buyer-declared sellers; brand.json authorization checked first) |
| POST | /mcp | This buyer's MCP endpoint (`get_adcp_capabilities`); public URL = `NXFLO_AGENT_URL` |
| GET | /.well-known/jwks.json | Request-signing public keys (empty when no key configured) |
| GET | /health | Health check |

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| NXFLO_HOST | 0.0.0.0 | Server bind host |
| NXFLO_PORT | 8000 | Server bind port |
| NXFLO_DATABASE_URL | sqlite+aiosqlite:///nxflo.db | Database connection |
| NXFLO_REGISTRY_URL | https://agenticadvertising.org/api/registry | Primary AdCP registry base URL |
| NXFLO_REGISTRY_FALLBACK_URL | https://adcontextprotocol.org/api/registry | Fallback registry (empty disables) |
| NXFLO_ADCP_VERSION | 3.2 | `adcp_version` sent on 3.2-shaped requests |
| NXFLO_MCP_MAX_RETRIES | 3 | Retries per operation (clamped to 3) |
| NXFLO_MCP_RETRY_BUDGET_SECONDS | 300 | Cumulative retry time per operation (clamped to 300) |
| NXFLO_BRAND_URL | https://nexflo.ai | Default advertiser; its host is the default brand domain |
| NXFLO_OPERATOR_DOMAIN | nxflo.io | Our operator domain (account natural key; must be in the advertiser's brand.json `authorized_operators`) |
| NXFLO_AGENT_URL | https://buyer.nxflo.io/mcp | Public URL of this buyer's MCP endpoint (must equal our brand.json `agents[].url`) |
| NXFLO_BRAND_JSON_URL | (derived: `https://{operator_domain}/.well-known/brand.json`) | `identity.brand_json_url` in our capabilities |
| NXFLO_AGENT_SUPPORTED_PROTOCOLS | ["media_buy"] | JSON list for our capabilities `supported_protocols` (schema needs >= 1; no buyer-only value exists yet) |
| NXFLO_ACCOUNT_MODE | auto | auto (from seller `require_operator_auth`) / buyer_declared / operator_authorized |
| NXFLO_ACCOUNT_BILLING | operator | Invoiced party for `sync_accounts` (operator / agent / advertiser); never remapped |
| NXFLO_ACCOUNT_SANDBOX | false | Use sandbox accounts (natural key `sandbox: true`) |
| NXFLO_ACCOUNT_AUTO_PROVISION | true | Run `sync_accounts` before the first billable call when needed |
| NXFLO_ACCOUNT_CURRENCY | (empty) | Account currency for sellers whose accounts are fixed-currency only |
| NXFLO_ACCOUNT_TIMEZONE | (empty) | Account timezone for sellers requiring a buyer-selected timezone |
| NXFLO_BRAND_CACHE_TTL_SECONDS / NXFLO_BRAND_NEGATIVE_CACHE_TTL_SECONDS | 86400 / 3600 | brand.json cache |
| NXFLO_SKIP_SALES_AGENT_AUTHORIZATION_CHECK | false | Sandbox only: skip the adagents.json check (logged loudly) |
| NXFLO_ADAGENTS_CACHE_TTL_SECONDS / NXFLO_ADAGENTS_NEGATIVE_CACHE_TTL_SECONDS | 86400 / 3600 | adagents.json cache |
| NXFLO_SIGNING_ENABLED | false | Sign tools/call requests the seller verifies (optional in AdCP 3.2) |
| NXFLO_SIGNING_PRIVATE_KEY_PATH | (empty) | PEM (PKCS#8) or private JWK file — never commit |
| NXFLO_SIGNING_PRIVATE_KEY | (empty) | Inline PEM/JWK (e.g. injected from a secret manager) |
| NXFLO_SIGNING_KEY_ID | (empty) | `kid` (required for PEM keys) |
| NXFLO_SIGNING_ADDITIONAL_JWKS_PATH | (empty) | Extra public keys to keep publishing during rotation |

Per-seller variables (`<SLUG>` = seller host upper-cased, non-alphanumerics → `_`, e.g. `TEST_AGENT_ADCONTEXTPROTOCOL_ORG`):

| Variable | Purpose |
|----------|---------|
| NXFLO_SELLER_TOKEN_<SLUG> | Our agent bearer token for that seller (`NXFLO_SELLER_TOKEN_ADCP_TEST` still works for the test agent) |
| NXFLO_OPERATOR_TOKEN_<SLUG> | Operator credential used instead of ours when the seller requires operator auth |
| NXFLO_OPERATOR_ACCOUNT_ID_<SLUG> | Pin the seller-assigned `account_id` (operator-authorized sellers) |
| NXFLO_ACCOUNT_MODE_<SLUG> | Per-seller account mode override |

## Key Patterns

- **MCP Client**: Uses `fastmcp.client.Client` with `StreamableHttpTransport`. Auth via `Authorization: Bearer` (operator credential in operator-authorized mode). Responses: structuredContent first, then JSON text, else `{"raw": text}`.
- **Accounts (3.2)**: buyer-declared sellers (`require_operator_auth: false`) are provisioned with `sync_accounts` and referenced by the **natural key** `{brand, operator: NXFLO_OPERATOR_DOMAIN[, sandbox]}` — a returned `account_id` is never sent. Operator-authorized sellers use `{account_id}` from `list_accounts` or `NXFLO_OPERATOR_ACCOUNT_ID_<SLUG>`. `get_products` omits `account` until it is provisioned. Account status gates calls (only `active` may `create_media_buy`).
- **Brand Protocol**: before `create_media_buy`/`sync_accounts`, the advertiser's brand.json must list our operator in `authorized_operators` (operator == brand domain is implicit self-authorization). Fails closed.
- **adagents.json**: every `product.publisher_properties[]` publisher must list the seller's exact agent URL with an in-window grant covering the claimed properties. Fails closed.
- **create_media_buy**: `idempotency_key`, `account`, `brand` (BrandRef), `budget_allocation: {"mode": "fixed"}` (or a configured `seller_optimized` block), `format_option_refs`, correlation in `context.buyer_ref`.
- **Retries**: transient errors only, <= 3 retries / <= 300 s per operation; mutations retried only with an `idempotency_key` on sellers declaring `adcp.idempotency.supported: true`.
- **Request signing**: when enabled, `tools/call` requests listed in the seller's `request_signing.supported_for/warn_for/required_for` are signed (3.2 profile, content-digest covered). Publish our `agents[]` entry in nxflo.io's brand.json (`src.agent_server.mcp.build_brand_json_agent_entry()`).
- **Task Lifecycle**: Operations can return `submitted` (long-running), `working` (< 120s), `input-required` (HITL), or `completed`/`failed`.

## Related Repos

- `adcp/` — AdCP protocol specification + server
- `nxflo-dsp/` — RTB DSP (execution engine behind seller agent)
- `nxflo-seller/` — Prebid Sales Agent (seller side)
