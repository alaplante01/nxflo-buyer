"""Nexflo Buyer — FastAPI entry point.

Discovers AdCP seller agents and buys inventory on behalf of advertisers.

Usage:
    python -m src.main
    # or
    uvicorn src.main:app --host 0.0.0.0 --port 8000
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.api import routes
from src.buying.orchestrator import BuyingOrchestrator
from src.buying.poller import BackgroundPoller
from src.config import settings
from src.connections.seller import tasks_get
from src.webhooks.receiver import router as webhook_router, set_tracker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize the orchestrator on startup, discover sellers."""
    logger.info("Nexflo Buyer starting...")

    # Create and initialize orchestrator
    orch = BuyingOrchestrator()
    routes.orchestrator = orch

    # Initialize SQLite database
    await orch.tracker.init_db()

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
            f"{protocols_tag} {s.url}"
        )
    sales = [s for s in sellers if s.can_sell]
    logger.info(f"  {len(sales)} sellers can sell, {len(sellers) - len(sales)} other agents")

    # Reconcile pending operations from previous runs
    pending = orch.tracker.get_pending()
    if pending:
        logger.info(f"Found {len(pending)} pending operations from previous run, reconciling...")
        for op in pending:
            if not op.task_id:
                continue
            seller = orch._find_seller_by_url(op.seller_url)
            if seller:
                try:
                    response = await tasks_get(seller, op.task_id, include_result=True)
                    orch.tracker.update_from_response(op.id, response)
                    await orch.tracker._persist(op)
                    logger.info(f"  Reconciled {op.id} -> {op.status.value}")
                except Exception as e:
                    logger.warning(f"  Reconciliation failed for {op.id}: {e}")

    # Start background poller
    poller = BackgroundPoller(orch.tracker, orch.sellers)
    await poller.start()

    yield

    # Shutdown
    await poller.stop()
    logger.info("Nexflo Buyer shutting down")


app = FastAPI(
    title="Nexflo Buyer",
    description="AdCP buying agent that discovers seller agents and purchases inventory",
    version="0.3.0",
    lifespan=lifespan,
)

app.include_router(routes.router)
app.include_router(webhook_router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "src.main:app",
        host=settings.host,
        port=settings.port,
        reload=True,
    )
