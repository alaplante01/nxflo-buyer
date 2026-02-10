"""ADFX Buying Agent — FastAPI entry point.

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
    logger.info("ADFX Buying Agent starting...")

    # Create and initialize orchestrator
    orch = BuyingOrchestrator()
    routes.orchestrator = orch

    # Initial seller discovery
    sellers = await orch.discover_sellers()
    logger.info(f"Discovered {len(sellers)} seller agents")
    for s in sellers:
        logger.info(f"  - {s.name} ({s.url}) [auth={'yes' if s.token else 'no'}]")

    yield

    logger.info("ADFX Buying Agent shutting down")


app = FastAPI(
    title="ADFX Buying Agent",
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
