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
from src.config import settings

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

    # Initial seller discovery with probing
    sellers = await orch.discover_sellers(probe=True)
    logger.info(f"Discovered {len(sellers)} seller agents")
    for s in sellers:
        status_tag = s.status.upper()
        auth_tag = "auth" if s.token else "no-auth"
        tools_tag = f"{len(s.tools)} tools" if s.tools else "unknown"
        logger.info(f"  - {s.name} [{status_tag}] [{auth_tag}] [{tools_tag}] {s.url}")
    sales = [s for s in sellers if s.can_sell]
    logger.info(f"  {len(sales)} sellers can sell, {len(sellers) - len(sales)} other agents")

    yield

    logger.info("Nexflo Buyer shutting down")


app = FastAPI(
    title="Nexflo Buyer",
    description="AdCP buying agent that discovers seller agents and purchases inventory",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(routes.router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "src.main:app",
        host=settings.host,
        port=settings.port,
        reload=True,
    )
