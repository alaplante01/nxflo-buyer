# CLAUDE.md

## Project Overview

Nexflo Buyer — An AdCP buying agent that discovers seller agents on the AdCP registry and purchases advertising inventory on behalf of advertisers. This is the **demand side** of the AdCP (Ad Context Protocol) ecosystem.

## Architecture

```
Advertiser/AI Client
       |
  Nexflo Buyer (this repo)
       |
       +-- calls seller agents via MCP (Model Context Protocol)
       +-- discovers sellers from AdCP registry
       +-- tracks async operations (submitted -> completed)
       +-- exposes REST API for campaign management
```

## Key Modules

- `src/discovery/registry.py` — Fetches sellers from AdCP registry + local config
- `src/connections/seller.py` — MCP client wrapper (FastMCP StreamableHttpTransport)
- `src/buying/orchestrator.py` — Core workflow: discover → get_products → rank → buy → monitor
- `src/buying/tracker.py` — Async task lifecycle state machine
- `src/api/routes.py` — FastAPI REST endpoints
- `src/main.py` — Entry point with lifespan management

## Commands

```bash
# Setup
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"

# Run
.venv/Scripts/python -m src.main

# Test (live, hits real AdCP test agent)
.venv/Scripts/python -m tests.test_live_agent

# Lint
.venv/Scripts/ruff check src/
```

## API Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | /discover | Discover seller agents |
| POST | /products | Search products across sellers |
| POST | /buy | Full buy workflow (discover → rank → purchase) |
| GET | /operations | List tracked operations |
| POST | /operations/poll | Poll pending operations |
| GET | /health | Health check |

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| NXFLO_HOST | 0.0.0.0 | Server bind host |
| NXFLO_PORT | 8000 | Server bind port |
| NXFLO_DATABASE_URL | sqlite+aiosqlite:///nxflo.db | Database connection |
| NXFLO_REGISTRY_URL | https://adcontextprotocol.org/api/registry | AdCP registry |

## Key Patterns

- **MCP Client**: Uses `fastmcp.client.Client` with `StreamableHttpTransport`. Auth via Bearer token headers.
- **Response Parsing**: Seller responses may be JSON or text. Always try JSON parse first, fall back to `{"raw": text}`.
- **Task Lifecycle**: Operations can return `submitted` (long-running), `working` (< 120s), `input-required` (HITL), or `completed`/`failed`.
- **Idempotency**: Use `buyer_ref` on create_media_buy for crash recovery.

## Related Repos

- `c:\Github\adcp\` — AdCP protocol specification + server
- `c:\Github\dsp-v2\` — RTB DSP (execution engine behind seller agent)
- `c:\Github\adfx\salesagent\` — Prebid Sales Agent (seller side)
