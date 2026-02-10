"""FastAPI routes for the ADFX Buying Agent."""

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
    brand_name: str = Field(default="ADFX")
    brand_url: str = Field(default="https://adfx.io")


class ProductSearchResponse(BaseModel):
    products: list[dict[str, Any]]
    count: int
    sellers_queried: int


class BuyRequest(BaseModel):
    brief: str = Field(..., description="What inventory to buy")
    budget: float = Field(..., gt=0, description="Budget in USD")
    brand_name: str = Field(default="ADFX")
    brand_url: str = Field(default="https://adfx.io")
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


# --- Endpoints ---


@router.get("/discover", response_model=DiscoverResponse)
async def discover_sellers():
    """Discover all available seller agents from registry + config.

    Probes each seller to discover tools and classify by type.
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
    """Poll all pending operations for status updates."""
    orch = get_orchestrator()
    results = await orch.poll_pending_operations()
    return {"polled": len(results), "results": results}


@router.get("/health")
async def health():
    return {"status": "ok", "service": "adfx-buyer"}
