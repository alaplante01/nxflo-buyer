# nxflo-buyer

nxflo-buyer is a buying agent for the Ad Context Protocol (AdCP): a Python/FastAPI service that finds seller agents in the AdCP registry, compares the products they offer, and places and tracks media buys with them over MCP. It is parked: it ran on AWS ECS Fargate in February 2026 and was shut down on 2026-02-25, and the AdCP 3.2.1 update from October 2026 has been tested locally but never deployed.

## Overview

AdCP is an open protocol that lets AI agents buy and sell advertising. Seller agents, run for publishers and sales houses, expose tasks such as `get_products`, `create_media_buy`, `sync_creatives` and `get_media_buy_delivery` over MCP or A2A; this agent uses MCP. A buying agent works for the advertiser: it finds sellers, sends them a brief, chooses products, buys, and follows each buy until it finishes.

This agent:

- discovers sellers from the AdCP registry (a primary registry, then a fallback) plus 9 endpoints configured in `src/config.py`, and probes each one (`list_tools`, then `get_adcp_capabilities` where the seller has it);
- sends `get_products` to every seller that has the tool, in parallel, and ranks the results by price;
- before `create_media_buy`, checks the advertiser's `brand.json`, each publisher's `adagents.json`, and the seller account (AdCP 3.2 accounts model);
- sends `create_media_buy` with an idempotency key and an explicit budget allocation, then tracks the operation with a background poller and, when a webhook URL is configured, HMAC-verified webhooks;
- routes seller tool calls (not the discovery probes) through a per-seller circuit breaker and a bounded retry budget;
- can sign MCP tool calls with RFC 9421 HTTP Message Signatures (Ed25519 or ECDSA P-256), publishes its public keys as a JWKS, and answers `get_adcp_capabilities` at its own `/mcp` endpoint;
- fetches `brand.json` and `adagents.json` through an HTTPS-only fetcher that refuses private addresses (SSRF guard).

Operators drive it through a REST API: `/discover`, `/products`, `/buy`, `/operations`, `/media-buy/*`, `/accounts/*`, `/signals/*`, `/publishers`, `/health` and `/metrics`.

`publisher/` holds publisher-side pieces of the wider ad stack: a Prebid.js wrapper and bid adapter, a WordPress plugin, and a load generator. They send traffic to the Prebid Server and DSP services, which live in other repositories; only the site ID they use comes from this agent's `/publishers` registration API.

## Status

| When | What | Where it is recorded |
| --- | --- | --- |
| Feb 2026 | Built and connected to live AdCP agents: 10 of 10 reachable over MCP, 8 of them sales-capable | commit 14f3e1d |
| Feb 2026 | Deployed on ECS Fargate (one task, 512 CPU units / 1024 MB) behind an ALB, with Aurora PostgreSQL Serverless v2 through RDS Proxy; Terraform and GitHub Actions | `deploy/terraform/`, `.github/workflows/deploy.yml` |
| 2026-02-25 | Parked. The snapshot records the configuration and the parking plan: delete the NAT gateway, load balancers and RDS proxies; snapshot and stop Aurora; keep images, IAM roles and secrets | `INFRASTRUCTURE-SNAPSHOT.md` |
| Oct 2026 | v0.5.0 targets AdCP 3.2.1 (accounts, Brand Protocol, `adagents.json`, request signing, retry budget). Tested locally, never deployed | `CHANGELOG.md`, commit ac7bb86 |

As of 2026-10-26 none of the service hostnames resolve, so nothing here is running. `INFRASTRUCTURE-SNAPSHOT.md` has the configuration at parking time and a spin-up checklist; its AWS account and resource IDs are placeholders.

## Architecture

Sellers come from the registry (`GET /agents` on the primary registry, then the fallback) plus the configured list. They are probed at startup and again on `GET /discover`. What `POST /buy` does with them:

```text
POST /buy {brief, budget, brand_domain, ...}
  1. discover   reuse the sellers found at startup or by GET /discover
  2. products   get_products to every seller that has the tool (asyncio.gather)
  3. rank       priced products first, then lowest CPM
  4. brand      advertiser brand.json must list our operator domain     -> else fail
  5. sellers    each publisher's adagents.json must list the seller URL  -> else fail
  6. account    natural key (buyer-declared) or account_id (operator-authorized)
  7. buy        create_media_buy with idempotency_key, budget_allocation, optional signature
  8. track      operation stored before step 7; poller and webhooks update it
```

| Path | Responsibility |
| --- | --- |
| `src/discovery/registry.py` | Registry fetch with fallback, configured sellers, capability probing |
| `src/connections/` | FastMCP Streamable HTTP client, `context_id` sessions, circuit breakers, retry budget |
| `src/buying/` | Orchestrator, AdCP 3.2.1 request builders, operation state machine, background poller |
| `src/accounts/` | `list_accounts` / `sync_accounts` and account references per seller mode |
| `src/brand/` | `brand.json` resolver and `authorized_operators` check |
| `src/authorization/` | Sales-agent authorization against publisher `adagents.json` |
| `src/signing/` | RFC 9421 signer, key loading, JWKS, key generator |
| `src/protocol/` | AdCP URL canonicalization, SSRF-guarded JSON fetcher |
| `src/webhooks/` | Push-notification config; HMAC/Bearer webhook receiver with dedup |
| `src/agent_server/` | The buyer's own MCP endpoint (`get_adcp_capabilities`) |
| `src/api/`, `src/middleware.py`, `src/metrics.py` | REST routes, publisher registration, correlation IDs, rate limit, Prometheus |
| `alembic/` | 4 migrations, 6 tables (operations, sellers, seller_capabilities, webhook_events, publishers, seller_accounts) |
| `deploy/terraform/` | VPC and endpoints, NAT, ECR, ECS Fargate, ALB and ACM, Aurora Serverless v2 and RDS Proxy, Route 53, Secrets Manager |

Storage is async SQLAlchemy. Migrations run at startup against a local SQLite file by default, or Aurora PostgreSQL (asyncpg) in AWS.

## Getting started

Requires Python 3.11 or later. At startup the app probes every configured and registry seller over the network before it serves requests; sellers it cannot reach are marked as errors.

```bash
python3.11 -m venv .venv
.venv/bin/pip install -e ".[dev]"     # or: uv venv --python 3.11 .venv && uv pip install -e ".[dev]"

.venv/bin/uvicorn src.main:app --port 8000
curl -s localhost:8000/health
curl -s localhost:8000/discover
curl -s -X POST localhost:8000/products -H 'content-type: application/json' \
     -d '{"brief": "Premium athletic footwear advertising"}'
```

With Docker: `docker build -t nxflo-buyer . && docker run -p 8000:8000 nxflo-buyer` (non-root user, health check on `/health`).

Settings are `NXFLO_*` environment variables. `src/config.py` defines the global ones; the table in `CLAUDE.md` lists all of them, including per-seller overrides such as `NXFLO_OPERATOR_TOKEN_<SLUG>`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `NXFLO_DATABASE_URL` | `sqlite+aiosqlite:///nxflo.db` | Use `postgresql+asyncpg://...` for PostgreSQL |
| `NXFLO_REGISTRY_URL`, `NXFLO_REGISTRY_FALLBACK_URL` | `https://agenticadvertising.org/api/registry`, `https://adcontextprotocol.org/api/registry` | Registry base URLs; an empty fallback disables it |
| `NXFLO_SELLER_TOKEN_<SLUG>` | unset | Bearer token for one seller; `<SLUG>` is the seller host upper-cased, other characters as `_` |
| `NXFLO_OPERATOR_DOMAIN` | `nxflo.io` | Operator identity checked against `brand.json` |
| `NXFLO_AGENT_URL` | `https://buyer.nxflo.io/mcp` | Public URL of this agent's `/mcp` endpoint |
| `NXFLO_WEBHOOK_BASE_URL`, `NXFLO_WEBHOOK_SECRET` | unset | Turns on push notifications; HMAC secret |
| `NXFLO_SIGNING_ENABLED`, `NXFLO_SIGNING_PRIVATE_KEY_PATH`, `NXFLO_SIGNING_KEY_ID` | off | RFC 9421 request signing |
| `NXFLO_SKIP_SALES_AGENT_AUTHORIZATION_CHECK` | `false` | Sandbox only: skips the `adagents.json` check and logs a warning |

To create a signing key (written with mode 0600, never overwritten; the public JWKS is printed):

```bash
mkdir -p ~/.nxflo
.venv/bin/python -m src.signing.keygen --kid buyer-2026-10 --private-out ~/.nxflo/signing-key.pem
```

## Tests

```bash
.venv/bin/python -m pytest -q --ignore=tests/test_live_agent.py   # offline suite
.venv/bin/python -m pytest -q                                      # adds the live tests
NXFLO_SELLER_TOKEN_ADCP_TEST=... .venv/bin/python -m pytest tests/test_live_agent.py -v -s
.venv/bin/ruff check src/ tests/
```

`grep "def test_"` finds 144 test functions in 12 modules. With parametrization, pytest collects 208 cases.

| Module | Cases | Covers |
| --- | --- | --- |
| `test_signing.py` | 61 | 41 URL-canonicalization vectors, 6 request-signing vectors, sign/verify round trips, JWKS, key generator, the signing auth hook |
| `test_unit.py` | 37 | Circuit breaker, webhook HMAC/Bearer checks, credential redaction, operation tracker, rate limiter |
| `test_adagents.py` | 23 | `adagents.json` authorization, including the fail-closed cases |
| `test_brand.py` | 20 | `brand.json` resolution, redirects, caching, `authorized_operators` |
| `test_accounts.py` | 15 | Buyer-declared and operator-authorized accounts, pagination, persistence |
| `test_media_buy_requests.py` | 15 | 3.2.1 request shapes, budget allocation, idempotency keys, buys refused before the seller is called |
| `test_retry.py` | 14 | Retry budget ceilings, recovery classes, `retry_after` |
| `test_safe_fetch.py` | 7 | HTTPS only, private-address refusal, redirect rules, size cap, strict JSON |
| `test_registry.py` | 6 | Registry entry parsing, primary and fallback |
| `test_live_agent.py` | 5 | Live: the public registry (2) and the AdCP test agent (3) |
| `test_agent_server.py` | 4 | The buyer's MCP endpoint and capabilities document |
| `test_app_boot.py` | 1 | Boots the real app offline: migrations, REST, MCP, JWKS |

The live tests read the AdCP test agent's token from `NXFLO_SELLER_TOKEN_ADCP_TEST` (the public test token published in the AdCP docs); the three that need it are skipped when it is unset. Five other files in `tests/` (`test_probe_sellers.py`, `test_cross_seller_products.py`, `test_extra_probes.py`, `test_list_tools.py`, `test_db_persistence.py`) are manual scripts with no test functions; run them from the repo root with `python -m tests.<name>`. All but the last call live seller agents.

CI (`.github/workflows/ci.yml`) runs `ruff check src/ tests/` and the offline suite on every push to `main` and on pull requests. `.github/workflows/deploy.yml` runs only when started by hand, because the stack is parked: it lints `src/`, runs `tests/test_unit.py`, builds the image, pushes it to ECR through an OIDC role and redeploys the ECS service.

## Results / what was measured

Measured on 2026-10-26 (Python 3.11.15, pytest 9.1.1, ruff 0.17.0):

- `pytest --ignore=tests/test_live_agent.py`: 203 passed in 0.94 s. The 5 live tests were not run.
- Those 203 include the 41 AdCP URL-canonicalization cases in `tests/fixtures/request_signing/canonicalization.json` and the 6 request-signing vectors next to it: the computed signature base matches each vector byte for byte, and the vector's signature verifies against the published public key.
- `ruff check src/ tests/`: no findings.

Recorded earlier and not re-run:

- Commit 14f3e1d (Feb 2026): 10 of 10 live AdCP agents reachable over MCP, 8 of them sales-capable.
- Commit ac7bb86 (Oct 2026): 205 tests passing when the 3.2.1 change was made.
- A recorded run of `publisher/stress-test.py`, cited from the message of commit 7f97625 (2026-02-22): "Validated 31K requests with 16K+ bids at <0.01% errors." That load went to the DSP and Prebid Server, not to this agent. The commit does not record the arguments, latency or client machine, and the run cannot be repeated while those hosts are offline.

The load generator is `python publisher/stress-test.py [test] [total] [concurrency]` (defaults `all 5000 500`). It needs `aiohttp`, which is not in `pyproject.toml`. Its nine scenarios:

1. `dsp`: DSP `/bid` with requests that match no campaign (throughput only)
2. `pbs`: Prebid Server to DSP pipeline (throughput only)
3. `match`: DSP `/bid` with requests that match the DSP's test campaigns, so real bids come back
4. `chain`: bid, then win notice, impression pixel and click redirect
5. `multi-imp`: 2 to 6 impressions per request
6. `mixed-media`: banner, video and native in one request
7. `cookie-sync`: Prebid Server `/cookie_sync`
8. `churn`: a new TCP connection for every request
9. `geo`: a sweep across matching and non-matching regions

Not measured: latency or throughput of this agent. The repo holds no spend or delivery data from live campaigns.

## Design notes

- **Fail closed before money moves.** A missing, unreachable or invalid `brand.json` or `adagents.json` blocks `create_media_buy`. The brand check is skipped only when the advertiser's domain is the operator domain itself. The publisher check is skipped only with `NXFLO_SKIP_SALES_AGENT_AUTHORIZATION_CHECK`, which logs a warning on every buy.
- **Store before sending.** The operation is written before `create_media_buy` goes out, and startup reconciles operations left pending by the previous run. A seller status the agent does not recognize becomes `unknown`, never `completed`.
- **Bounded retries.** At most 3 retries and 300 s per operation (the AdCP ceilings; settings are clamped to them), transient errors only, `retry_after` honored, jittered backoff otherwise. Mutations are retried only with an idempotency key, and only on sellers that declare idempotency support.
- **Per-seller circuit breakers.** Five consecutive failures open the circuit; after 60 s a single probe request is let through.
- **Signing is opt-in.** AdCP 3.2 makes request signing optional, so it sits behind `NXFLO_SIGNING_ENABLED`. It signs `tools/call` requests only for sellers on AdCP 3.2 or later that advertise signature verification, and only for the operations they list (plus calls that carry webhook credentials). Keys load from an env var or file. The JWKS is published as soon as a key is configured, so verifiers can cache it before signing starts.
- **SSRF-guarded trust-root fetches.** HTTPS only; private, loopback, link-local and reserved addresses refused at every redirect hop; a redirect policy per document type; a streamed size cap; strict JSON (duplicate keys rejected, nesting depth bounded).
- **Simple ranking.** Lowest CPM first. No model is involved in the buy decision.
- **Observability.** JSON logs with correlation IDs, Prometheus metrics labelled by route template rather than raw path, circuit-breaker state in `/health`.
- Development used Claude Code as a pair-programmer; `CLAUDE.md` is the working guide for it.

## Limitations

- The REST API has no authentication (`KNOWN_ISSUES.md`, H1). Anyone who can reach it can trigger purchases, so it must not be exposed as it is.
- `POST /buy` is not idempotent across client retries: every call generates a new idempotency key, and `buyer_ref` is only a correlation id.
- Each MCP call opens a new connection (H2), the rate limiter is per process (H3), and the poller has no backoff (L8). `KNOWN_ISSUES.md` lists the rest.
- The SSRF guard resolves DNS to validate a host but does not pin the connection to that address, so a DNS-rebinding race remains.
- Inbound webhooks are checked with HMAC-SHA256 or a bearer token; AdCP 3.2 signed-webhook verification is not implemented. Also outside the 3.2.1 change (commit ac7bb86): the signals field renames, the webhook authentication block migration, `sync_principal`, and the ads.txt `managerdomain` fallback.
- CI skips the live tests and does not lint `publisher/` or `alembic/`, which have 47 ruff findings under ruff 0.17.0. Dependencies have lower bounds only and there is no lock file, so CI can install newer versions than the ones measured above.
- The host in the default `NXFLO_AGENT_URL` and the hostnames hard-coded in `publisher/` do not resolve, so those defaults must be overridden before use.

## License

MIT; see `LICENSE`. Two exceptions: `publisher/wordpress-plugin/` declares GPL-2.0-or-later in its plugin header and `readme.txt`, and `tests/fixtures/request_signing/` holds AdCP 3.2.1 test vectors copied from github.com/adcontextprotocol/adcp at commit ec882d2 (`public-keys.json` is two of the keys in its `keys.json`), which are Apache-2.0; that licence is in the same directory.
