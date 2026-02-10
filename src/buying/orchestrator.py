"""Core buying orchestrator.

Workflow: discover sellers -> get products -> rank -> create media buy -> monitor delivery.

This is the brain of the buying agent. It coordinates across multiple seller
agents to find the best inventory for an advertiser's brief.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from src.config import settings
from src.connections.seller import (
    call_seller_tool,
    create_media_buy_on_seller,
    get_delivery,
    get_seller_capabilities,
    get_seller_products,
)
from src.discovery.registry import SellerAgent, discover_all_sellers
from src.buying.tracker import OperationTracker, TaskStatus

logger = logging.getLogger(__name__)


@dataclass
class SellerProduct:
    """A product from a seller, enriched with seller context."""

    seller: SellerAgent
    product_id: str
    name: str
    description: str = ""
    price_cpm: float | None = None
    channels: list[str] = field(default_factory=list)
    formats: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


@dataclass
class BuyResult:
    """Result of a buying operation."""

    operation_id: str
    seller_name: str
    status: str
    media_buy_id: str | None = None
    task_id: str | None = None
    error: str | None = None
    raw_response: dict = field(default_factory=dict)


class BuyingOrchestrator:
    """Orchestrates buying across multiple seller agents."""

    def __init__(self):
        self.tracker = OperationTracker()
        self._sellers: list[SellerAgent] = []
        self._last_discovery: float = 0

    async def discover_sellers(self, force: bool = False) -> list[SellerAgent]:
        """Discover all available seller agents."""
        self._sellers = await discover_all_sellers()
        return self._sellers

    @property
    def sellers(self) -> list[SellerAgent]:
        return self._sellers

    async def get_products_from_all(
        self, brief: str, brand_name: str | None = None, brand_url: str | None = None
    ) -> list[SellerProduct]:
        """Fan out get_products to all sellers, collect and normalize results."""
        if not self._sellers:
            await self.discover_sellers()

        brand = brand_name or settings.brand_name
        url = brand_url or settings.brand_url

        # Fan out to all sellers concurrently
        tasks = []
        for seller in self._sellers:
            tasks.append(self._get_products_from_seller(seller, brief, brand, url))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Collect all products
        all_products: list[SellerProduct] = []
        for seller, result in zip(self._sellers, results):
            if isinstance(result, Exception):
                logger.warning(f"Failed to get products from {seller.name}: {result}")
                continue
            all_products.extend(result)

        logger.info(f"Found {len(all_products)} products across {len(self._sellers)} sellers")
        return all_products

    async def _get_products_from_seller(
        self, seller: SellerAgent, brief: str, brand_name: str, brand_url: str
    ) -> list[SellerProduct]:
        """Get products from a single seller and normalize."""
        try:
            response = await get_seller_products(seller, brief, brand_name, brand_url)
        except Exception as e:
            logger.warning(f"Error from {seller.name}: {e}")
            return []

        products: list[SellerProduct] = []
        raw_products = response.get("products", [])

        for p in raw_products:
            # Extract price from pricing_options
            price_cpm = None
            for pricing in p.get("pricing_options", []):
                if pricing.get("pricing_model") == "cpm":
                    price_cpm = pricing.get("rate")
                    break

            products.append(
                SellerProduct(
                    seller=seller,
                    product_id=p.get("product_id", ""),
                    name=p.get("name", "Unknown"),
                    description=p.get("description", ""),
                    price_cpm=price_cpm,
                    channels=p.get("channels", []),
                    formats=[f.get("format_id", "") for f in p.get("formats", [])],
                    raw=p,
                )
            )

        return products

    def rank_products(self, products: list[SellerProduct]) -> list[SellerProduct]:
        """Rank products by relevance and value.

        MVP ranking: sort by price (lowest CPM first), with products
        that have pricing info ranked above those without.
        """
        def sort_key(p: SellerProduct) -> tuple[int, float]:
            has_price = 0 if p.price_cpm is not None else 1
            price = p.price_cpm if p.price_cpm is not None else 999.0
            return (has_price, price)

        return sorted(products, key=sort_key)

    async def buy(
        self,
        product: SellerProduct,
        budget: float,
        buyer_ref: str | None = None,
        end_time: str | None = None,
    ) -> BuyResult:
        """Execute a media buy on a specific product.

        Returns a BuyResult with the operation status and IDs.
        """
        ref = buyer_ref or f"adfx-{uuid.uuid4().hex[:12]}"
        brand_manifest = {"name": settings.brand_name, "url": settings.brand_url}

        # Track the operation
        op = self.tracker.create(
            operation_type="create_media_buy",
            seller_name=product.seller.name,
            seller_url=product.seller.url,
            buyer_ref=ref,
            request_data={
                "product_id": product.product_id,
                "budget": budget,
                "brand_manifest": brand_manifest,
            },
        )

        try:
            response = await create_media_buy_on_seller(
                agent=product.seller,
                product_id=product.product_id,
                budget=budget,
                buyer_ref=ref,
                brand_manifest=brand_manifest,
                end_time=end_time,
            )

            op = self.tracker.update_from_response(op.id, response)

            return BuyResult(
                operation_id=op.id,
                seller_name=product.seller.name,
                status=op.status.value,
                media_buy_id=op.media_buy_id,
                task_id=op.task_id,
                raw_response=response,
            )

        except Exception as e:
            self.tracker.mark_failed(op.id, str(e))
            return BuyResult(
                operation_id=op.id,
                seller_name=product.seller.name,
                status="failed",
                error=str(e),
            )

    async def check_delivery(
        self, seller: SellerAgent, media_buy_id: str
    ) -> dict[str, Any]:
        """Check delivery metrics for a media buy."""
        return await get_delivery(seller, media_buy_id)

    async def poll_pending_operations(self) -> list[dict]:
        """Poll all pending operations for status updates."""
        pending = self.tracker.get_pending()
        results = []

        for op in pending:
            if not op.task_id:
                continue

            # Find the seller for this operation
            seller = next(
                (s for s in self._sellers if s.url.rstrip("/") == op.seller_url.rstrip("/")),
                None,
            )
            if not seller:
                continue

            try:
                response = await call_seller_tool(
                    seller, "tasks/get", {"task_id": op.task_id, "include_result": True}
                )
                op = self.tracker.update_from_response(op.id, response)
                op.poll_count += 1
                results.append({"operation_id": op.id, "status": op.status.value})
            except Exception as e:
                logger.warning(f"Failed to poll operation {op.id}: {e}")

        return results
