"""Nexflo Buyer — FastAPI entry point.

Discovers AdCP seller agents and buys inventory on behalf of advertisers.

Also serves this buyer's own AdCP surface:
- POST /mcp                    MCP endpoint answering get_adcp_capabilities
                               (public URL = NXFLO_AGENT_URL)
- GET  /.well-known/jwks.json  request-signing public keys (RFC 9421 verifiers)

Usage:
    python -m src.main
    # or
    uvicorn src.main:app --host 0.0.0.0 --port 8000
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from src.agent_server.mcp import create_agent_mcp
from src.api import routes
from src.api.publishers import router as publishers_router
from src.buying.orchestrator import BuyingOrchestrator
from src.buying.poller import BackgroundPoller
from src.config import settings
from src.logging_config import setup_logging
from src.middleware import CorrelationIdMiddleware, RateLimitMiddleware
from src.signing.keys import build_jwks, load_signing_key_from_settings
from src.webhooks.receiver import router as webhook_router
from src.webhooks.receiver import set_tracker

setup_logging()
logger = logging.getLogger(__name__)

agent_mcp_app = create_agent_mcp().http_app(path="/mcp", stateless_http=True, json_response=True)


def _check_signing_config() -> None:
    """Fail fast when signing is enabled without usable key material."""
    if not settings.signing_enabled:
        return
    key = load_signing_key_from_settings()
    if key is None:
        raise RuntimeError(
            "NXFLO_SIGNING_ENABLED=true but no key configured "
            "(NXFLO_SIGNING_PRIVATE_KEY_PATH or NXFLO_SIGNING_PRIVATE_KEY)"
        )
    logger.info(f"Request signing enabled with kid={key.kid} alg={key.alg}")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialize the orchestrator on startup, discover sellers."""
    logger.info("Nexflo Buyer starting...")
    _check_signing_config()

    # Create and initialize orchestrator
    orch = BuyingOrchestrator()
    routes.orchestrator = orch

    # Initialize database (migrations) and load persisted state
    await orch.tracker.init_db()
    await orch.accounts.init_db()

    # Set tracker reference for webhook receiver
    set_tracker(orch.tracker)

    # Initial seller discovery with probing (including get_adcp_capabilities)
    sellers = await orch.discover_sellers(probe=True)
    logger.info(f"Discovered {len(sellers)} seller agents")
    for s in sellers:
        status_tag = s.status.upper()
        auth_tag = "auth" if s.token else "no-auth"
        tools_tag = f"{len(s.tools)} tools" if s.tools else "unknown"
        protocols_tag = f"protocols={s.supported_protocols}" if s.supported_protocols else ""
        logger.info(
            f"  - {s.name} [{status_tag}] [{auth_tag}] [{tools_tag}] "
            f"[accounts={s.account_mode.value}] {protocols_tag} {s.url}"
        )
    sales = [s for s in sellers if s.can_sell]
    logger.info(f"  {len(sales)} sellers can sell, {len(sellers) - len(sales)} other agents")

    # Reconcile pending operations from previous runs
    pending = orch.tracker.get_pending()
    if pending:
        logger.info(f"Found {len(pending)} pending operations from previous run, reconciling...")
        results = await orch.poll_pending_operations()
        for r in results:
            logger.info(f"  Reconciled {r['operation_id']} -> {r['status']}")

    # Start background poller (uses callable for fresh seller list)
    poller = BackgroundPoller(orch.tracker, lambda: orch.sellers)
    await poller.start()

    # The mounted MCP app needs its own lifespan (session manager task group)
    async with agent_mcp_app.lifespan(app):
        yield

    # Shutdown
    await poller.stop()
    logger.info("Nexflo Buyer shutting down")


app = FastAPI(
    title="Nexflo Buyer",
    description="AdCP buying agent that discovers seller agents and purchases inventory",
    version="0.5.0",
    lifespan=lifespan,
)

app.add_middleware(RateLimitMiddleware, rate=settings.rate_limit_per_minute, window=60.0)
app.add_middleware(CorrelationIdMiddleware)
app.include_router(routes.router)
app.include_router(webhook_router)
app.include_router(publishers_router)


@app.get("/metrics", include_in_schema=False)
async def metrics() -> PlainTextResponse:
    """Prometheus metrics endpoint."""
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/.well-known/jwks.json", include_in_schema=False)
async def jwks() -> JSONResponse:
    """Public request-signing keys (adcp_use: request-signing); empty when unconfigured.

    Keys are published whenever key material is configured, even before
    NXFLO_SIGNING_ENABLED flips, so verifiers can cache a new key ahead of use.
    """
    return JSONResponse(
        build_jwks(load_signing_key_from_settings()),
        headers={"Cache-Control": "public, max-age=3600"},
    )


# Mounted last so every route above takes precedence; serves POST/DELETE /mcp only.
app.mount("/", agent_mcp_app)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "src.main:app",
        host=settings.host,
        port=settings.port,
        reload=True,
    )
