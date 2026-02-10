"""FastAPI routes for the Nexflo buying agent.

Phase 2: Adds endpoints for HITL, media buy updates, creatives,
performance feedback, signals, and single operation detail.
"""

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from src.buying.orchestrator import BuyingOrchestrator

router = APIRouter()

# Singleton orchestrator (initialized in main.py lifespan)
orchestrator: BuyingOrchestrator | None = None


def get_orchestrator() -> BuyingOrchestrator:
    if orchestrator is None:
        raise HTTPException(status_code=503, detail="Orchestrator not initialized")
    return orchestrator


# --- Request/Response Models ---


class DiscoverResponse(BaseModel):
    sellers: list[dict[str, Any]]
    count: int


class ProductSearchRequest(BaseModel):
    brief: str = Field(..., description="Natural language description of what you want to buy")
    brand_name: str = Field(default="Nexflo")
    brand_url: str = Field(default="https://nexflo.io")


class ProductSearchResponse(BaseModel):
    products: list[dict[str, Any]]
    count: int
    sellers_queried: int


class BuyRequest(BaseModel):
    brief: str = Field(..., description="What inventory to buy")
    budget: float = Field(..., gt=0, description="Budget in USD")
    brand_name: str = Field(default="Nexflo")
    brand_url: str = Field(default="https://nexflo.io")
    buyer_ref: str | None = Field(default=None, description="Idempotency key")
    end_time: str | None = Field(default=None, description="Campaign end (ISO 8601)")
    product_index: int = Field(default=0, description="Which ranked product to buy (0 = best)")


class BuyResponse(BaseModel):
    operation_id: str
    seller_name: str
    status: str
    media_buy_id: str | None = None
    task_id: str | None = None
    error: str | None = None


class OperationsResponse(BaseModel):
    operations: list[dict[str, Any]]
    count: int


class ProvideInputRequest(BaseModel):
    input_data: str | dict = Field(..., description="Input to provide (string or structured dict)")


class UpdateMediaBuyRequest(BaseModel):
    seller_url: str = Field(..., description="Seller agent URL")
    media_buy_id: str | None = Field(default=None)
    buyer_ref: str | None = Field(default=None)
    paused: bool | None = Field(default=None)
    end_time: str | None = Field(default=None)
    packages: list[dict] | None = Field(default=None)


class SyncCreativesRequest(BaseModel):
    seller_url: str = Field(..., description="Seller agent URL")
    media_buy_id: str | None = Field(default=None)
    creatives: list[dict] = Field(..., description="Creative assets to sync")


class PerformanceFeedbackRequest(BaseModel):
    seller_url: str = Field(..., description="Seller agent URL")
    media_buy_id: str = Field(..., description="Media buy ID")
    performance_index: float = Field(..., description="Performance score (0-1)")
    measurement_period: dict = Field(..., description="Measurement period")


class SignalsDiscoverRequest(BaseModel):
    seller_url: str = Field(..., description="Signals agent URL")
    brief: str = Field(..., description="What signals to discover")
    platforms: list[dict] | None = Field(default=None)


class SignalActivateRequest(BaseModel):
    seller_url: str = Field(..., description="Signals agent URL")
    signal_id: str = Field(..., description="Signal ID to activate")
    platform: dict = Field(..., description="Platform to activate on")


# --- Endpoints ---


@router.get("/discover", response_model=DiscoverResponse)
async def discover_sellers():
    """Discover all available seller agents from registry + config.

    Probes each seller to discover tools, capabilities, and server card.
    """
    orch = get_orchestrator()
    sellers = await orch.discover_sellers(probe=True)
    return DiscoverResponse(
        sellers=[
            {
                "name": s.name,
                "url": s.url,
                "type": s.agent_type,
                "status": s.status,
                "source": s.source,
                "has_auth": s.token is not None,
                "tools": s.tools,
                "can_sell": s.can_sell,
                "supported_protocols": s.supported_protocols,
                "extensions_supported": s.extensions_supported,
                "adcp_versions": s.adcp_versions,
            }
            for s in sellers
        ],
        count=len(sellers),
    )


@router.post("/products", response_model=ProductSearchResponse)
async def search_products(req: ProductSearchRequest):
    """Search for products across all sellers matching a brief."""
    orch = get_orchestrator()
    products = await orch.get_products_from_all(req.brief, req.brand_name, req.brand_url)
    ranked = orch.rank_products(products)

    return ProductSearchResponse(
        products=[
            {
                "rank": i,
                "seller": p.seller.name,
                "product_id": p.product_id,
                "name": p.name,
                "description": p.description,
                "price_cpm": p.price_cpm,
                "channels": p.channels,
                "formats": p.formats,
            }
            for i, p in enumerate(ranked)
        ],
        count=len(ranked),
        sellers_queried=len(orch.sellers),
    )


@router.post("/buy", response_model=BuyResponse)
async def buy_inventory(req: BuyRequest):
    """Search products, rank them, and buy the best match.

    The full workflow:
    1. Search all sellers for products matching the brief
    2. Rank by price/relevance
    3. Create a media buy on the top-ranked product (or product_index)
    """
    orch = get_orchestrator()

    # Step 1: Get and rank products
    products = await orch.get_products_from_all(req.brief, req.brand_name, req.brand_url)
    if not products:
        raise HTTPException(status_code=404, detail="No products found matching brief")

    ranked = orch.rank_products(products)
    if req.product_index >= len(ranked):
        raise HTTPException(
            status_code=400,
            detail=f"product_index {req.product_index} out of range (found {len(ranked)} products)",
        )

    # Step 2: Buy
    selected = ranked[req.product_index]
    result = await orch.buy(
        product=selected,
        budget=req.budget,
        buyer_ref=req.buyer_ref,
        end_time=req.end_time,
    )

    return BuyResponse(
        operation_id=result.operation_id,
        seller_name=result.seller_name,
        status=result.status,
        media_buy_id=result.media_buy_id,
        task_id=result.task_id,
        error=result.error,
    )


# --- Operations ---


@router.get("/operations", response_model=OperationsResponse)
async def list_operations():
    """List all tracked buying operations."""
    orch = get_orchestrator()
    ops = orch.tracker.list_all()
    return OperationsResponse(
        operations=[
            {
                "id": op.id,
                "type": op.operation_type,
                "seller": op.seller_name,
                "status": op.status.value,
                "buyer_ref": op.buyer_ref,
                "media_buy_id": op.media_buy_id,
                "task_id": op.task_id,
                "error": op.error,
                "created_at": op.created_at.isoformat(),
                "updated_at": op.updated_at.isoformat(),
            }
            for op in ops
        ],
        count=len(ops),
    )


@router.post("/operations/poll")
async def poll_pending():
    """Poll all pending operations for status updates using tasks/get."""
    orch = get_orchestrator()
    results = await orch.poll_pending_operations()
    return {"polled": len(results), "results": results}


# --- Human-in-the-Loop ---


@router.get("/operations/input-required")
async def list_input_required():
    """List operations awaiting human input."""
    orch = get_orchestrator()
    ops = orch.tracker.get_input_required()
    return {
        "operations": [
            {
                "id": op.id,
                "type": op.operation_type,
                "seller": op.seller_name,
                "message": op.input_required_message,
                "data": op.input_required_data,
                "created_at": op.created_at.isoformat(),
            }
            for op in ops
        ],
        "count": len(ops),
    }


@router.post("/operations/{operation_id}/input")
async def provide_input(operation_id: str, req: ProvideInputRequest):
    """Provide input for an input-required operation to resume it."""
    orch = get_orchestrator()
    try:
        response = await orch.provide_input(operation_id, req.input_data)
        op = orch.tracker.get(operation_id)
        return {
            "operation_id": operation_id,
            "status": op.status.value if op else "unknown",
            "response": response,
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# --- Media Buy Management ---


@router.post("/media-buy/update")
async def update_media_buy(req: UpdateMediaBuyRequest):
    """Update an existing media buy (pause, change end date, etc.)."""
    orch = get_orchestrator()
    try:
        return await orch.update_media_buy_op(
            seller_url=req.seller_url,
            media_buy_id=req.media_buy_id,
            buyer_ref=req.buyer_ref,
            paused=req.paused,
            end_time=req.end_time,
            packages=req.packages,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/media-buy/creatives/sync")
async def sync_creatives(req: SyncCreativesRequest):
    """Upload/sync creative assets for a media buy."""
    orch = get_orchestrator()
    try:
        return await orch.sync_creatives_op(
            seller_url=req.seller_url,
            creatives=req.creatives,
            media_buy_id=req.media_buy_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/media-buy/feedback")
async def provide_feedback(req: PerformanceFeedbackRequest):
    """Share performance feedback with a seller."""
    orch = get_orchestrator()
    try:
        return await orch.provide_feedback_op(
            seller_url=req.seller_url,
            media_buy_id=req.media_buy_id,
            performance_index=req.performance_index,
            measurement_period=req.measurement_period,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# --- Signals ---


@router.post("/signals/discover")
async def discover_signals(req: SignalsDiscoverRequest):
    """Discover audience signals from a signals agent."""
    orch = get_orchestrator()
    try:
        return await orch.get_signals_op(
            seller_url=req.seller_url,
            brief=req.brief,
            platforms=req.platforms,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/signals/activate")
async def activate_signal(req: SignalActivateRequest):
    """Activate a signal for use in campaigns."""
    orch = get_orchestrator()
    try:
        return await orch.activate_signal_op(
            seller_url=req.seller_url,
            signal_id=req.signal_id,
            platform=req.platform,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# --- Health ---


@router.get("/operations/{operation_id}")
async def get_operation(operation_id: str):
    """Get a single operation with full detail."""
    orch = get_orchestrator()
    op = orch.tracker.get(operation_id)
    if not op:
        raise HTTPException(status_code=404, detail=f"Operation {operation_id} not found")

    return {
        "id": op.id,
        "type": op.operation_type,
        "seller": op.seller_name,
        "seller_url": op.seller_url,
        "status": op.status.value,
        "buyer_ref": op.buyer_ref,
        "media_buy_id": op.media_buy_id,
        "task_id": op.task_id,
        "context_id": op.context_id,
        "error": op.error,
        "poll_count": op.poll_count,
        "request_data": op.request_data,
        "response_data": op.response_data,
        "application_context": op.application_context,
        "webhook_config": op.webhook_config,
        "input_required_message": op.input_required_message,
        "input_required_data": op.input_required_data,
        "created_at": op.created_at.isoformat(),
        "updated_at": op.updated_at.isoformat(),
    }


# --- Health ---


@router.get("/health")
async def health():
    return {"status": "ok", "service": "nxflo-buyer", "version": "0.2.0"}
