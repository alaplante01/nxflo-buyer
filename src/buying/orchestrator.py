"""Core buying orchestrator.

Workflow: discover sellers -> get products -> rank -> create media buy -> monitor delivery.

This is the brain of the buying agent. It coordinates across multiple seller
agents to find the best inventory for an advertiser's brief.

All seller interactions route through SellerSession for automatic context_id
management across multi-turn conversations.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from src.config import settings
from src.connections.session import SellerSession
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
    format_ids: list[dict] = field(default_factory=list)  # AdCP FormatID objects: [{agent_url, id}]
    pricing_option_id: str | None = None
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
        self._seller_sessions: dict[str, SellerSession] = {}
        self._last_discovery: float = 0

    def get_seller_session(self, seller: SellerAgent) -> SellerSession:
        """Get or create a session for a seller (maintains context_id)."""
        key = seller.url.rstrip("/")
        if key not in self._seller_sessions:
            self._seller_sessions[key] = SellerSession(seller)
        return self._seller_sessions[key]

    async def _session_call(self, seller: SellerAgent, tool: str, params: dict[str, Any], **kwargs) -> dict[str, Any]:
        """Call a tool via the seller's session (context_id tracked automatically)."""
        session = self.get_seller_session(seller)
        return await session.call_with_retry(tool, params, **kwargs)

    async def discover_sellers(self, probe: bool = True) -> list[SellerAgent]:
        """Discover all available seller agents.

        If probe=True, connects to each seller to discover tools, capabilities,
        and server card metadata.
        """
        self._sellers = await discover_all_sellers(probe=probe)
        return self._sellers

    @property
    def sellers(self) -> list[SellerAgent]:
        return self._sellers

    @property
    def sales_sellers(self) -> list[SellerAgent]:
        """Sellers that support the media buy workflow."""
        return [s for s in self._sellers if s.can_sell]

    def _find_seller_by_url(self, url: str) -> SellerAgent | None:
        """Find a seller agent by URL."""
        url = url.rstrip("/")
        for s in self._sellers:
            if s.url.rstrip("/") == url:
                return s
        return None

    # --- Product Discovery ---

    async def get_products_from_all(
        self, brief: str, brand_name: str | None = None, brand_url: str | None = None
    ) -> list[SellerProduct]:
        """Fan out get_products to all sales-capable sellers, collect and normalize results."""
        if not self._sellers:
            await self.discover_sellers()

        brand = brand_name or settings.brand_name
        url = brand_url or settings.brand_url

        # Only query sellers that have get_products
        eligible = [s for s in self._sellers if "get_products" in s.tools]
        if not eligible:
            logger.warning("No sellers with get_products capability found")
            eligible = self._sellers  # Fallback: try all

        # Fan out to all eligible sellers concurrently
        tasks = []
        for seller in eligible:
            tasks.append(self._get_products_from_seller(seller, brief, brand, url))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Collect all products
        all_products: list[SellerProduct] = []
        for seller, result in zip(eligible, results):
            if isinstance(result, Exception):
                logger.warning(f"Failed to get products from {seller.name}: {result}")
                continue
            all_products.extend(result)

        logger.info(f"Found {len(all_products)} products across {len(eligible)} sellers")
        return all_products

    async def _get_products_from_seller(
        self, seller: SellerAgent, brief: str, brand_name: str, brand_url: str
    ) -> list[SellerProduct]:
        """Get products from a single seller and normalize."""
        try:
            response = await self._session_call(seller, "get_products", {
                "brief": brief,
                "brand_manifest": {"name": brand_name, "url": brand_url},
            })
        except Exception as e:
            logger.warning(f"Error from {seller.name}: {e}")
            return []

        products: list[SellerProduct] = []

        # Handle text-only responses (some sellers return summaries without auth)
        if "raw" in response and "products" not in response:
            logger.info(f"{seller.name} returned text response: {response['raw'][:100]}")
            return []

        raw_products = response.get("products", [])
        for p in raw_products:
            # Extract price and pricing_option_id from pricing_options
            price_cpm = None
            pricing_option_id = None
            for pricing in p.get("pricing_options", []):
                if pricing.get("pricing_model") == "cpm":
                    price_cpm = pricing.get("rate")
                    pricing_option_id = pricing.get("pricing_option_id")
                    break
            # Fall back to first pricing option if no CPM found
            if not pricing_option_id and p.get("pricing_options"):
                pricing_option_id = p["pricing_options"][0].get("pricing_option_id")

            # Extract format_ids (AdCP spec: list of {agent_url, id} objects)
            raw_format_ids = p.get("format_ids", [])
            if not raw_format_ids:
                # Fallback: reconstruct from legacy formats array
                for f in p.get("formats", []):
                    if isinstance(f, dict):
                        fid = f.get("format_id") or f.get("id", "")
                        if fid:
                            raw_format_ids.append({
                                "agent_url": f.get("agent_url", seller.url),
                                "id": fid,
                            })

            products.append(
                SellerProduct(
                    seller=seller,
                    product_id=p.get("product_id", ""),
                    name=p.get("name", "Unknown"),
                    description=p.get("description", ""),
                    price_cpm=price_cpm,
                    pricing_option_id=pricing_option_id,
                    channels=p.get("channels", []),
                    formats=[f.get("format_id", "") for f in p.get("formats", []) if isinstance(f, dict)],
                    format_ids=raw_format_ids,
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

    # --- Media Buy Operations ---

    async def buy(
        self,
        product: SellerProduct,
        budget: float,
        buyer_ref: str | None = None,
        end_time: str | None = None,
        packages: list[dict] | None = None,
        targeting: dict | None = None,
        pacing: str | None = None,
        proposal_id: str | None = None,
        currency: str = "USD",
    ) -> BuyResult:
        """Execute a media buy on a specific product.

        Supports:
        - Single product buy (default): builds one package from the product
        - Multi-package: pass custom packages list
        - Proposal workflow: pass proposal_id + budget (no packages needed)
        - Targeting overlay: geo, device, frequency caps per the AdCP spec
        - Pacing control: even (default), asap, front_loaded
        """
        ref = buyer_ref or f"nxflo-{uuid.uuid4().hex[:12]}"
        brand_manifest = {"name": settings.brand_name, "url": settings.brand_url}

        if not end_time:
            end_time = (datetime.now(UTC) + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Track the operation (persisted immediately for crash recovery)
        op = await self.tracker.create(
            operation_type="create_media_buy",
            seller_name=product.seller.name,
            seller_url=product.seller.url,
            buyer_ref=ref,
            request_data={
                "product_id": product.product_id,
                "budget": budget,
                "brand_manifest": brand_manifest,
                "proposal_id": proposal_id,
            },
        )

        # Build webhook config if base URL is set
        push_config = None
        if settings.webhook_base_url:
            from src.webhooks.config import build_push_notification_config
            push_config = build_push_notification_config(
                task_type="create_media_buy",
                operation_id=op.id,
            )
            op.webhook_config = push_config

        # Build params
        params: dict[str, Any] = {
            "buyer_ref": ref,
            "brand_manifest": brand_manifest,
            "start_time": "asap",
            "end_time": end_time,
        }

        if proposal_id:
            # Proposal workflow: seller already has the media plan
            params["proposal_id"] = proposal_id
            params["total_budget"] = {"amount": budget, "currency": currency}
        elif packages:
            # Multi-package: caller provides full package list
            for pkg in packages:
                pkg.setdefault("buyer_ref", ref)
            params["packages"] = packages
        else:
            # Single product: build one package
            pkg: dict[str, Any] = {
                "product_id": product.product_id,
                "budget": budget,
                "buyer_ref": ref,
                "pricing_option_id": product.pricing_option_id or "cpm-standard",
            }
            if product.format_ids:
                pkg["format_ids"] = product.format_ids
            if targeting:
                pkg["targeting_overlay"] = targeting
            if pacing:
                pkg["pacing"] = pacing
            params["packages"] = [pkg]

        if push_config:
            params["push_notification_config"] = push_config

        # Attach reporting webhook for automated delivery data
        if settings.webhook_base_url:
            from src.webhooks.config import build_reporting_webhook
            params["reporting_webhook"] = build_reporting_webhook(operation_id=op.id)

        try:
            session = self.get_seller_session(product.seller)
            response = await session.call_with_retry("create_media_buy", params)

            op = self.tracker.update_from_response(op.id, response)
            await self.tracker._persist(op)

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
            await self.tracker._persist(op)
            return BuyResult(
                operation_id=op.id,
                seller_name=product.seller.name,
                status="failed",
                error=str(e),
            )

    async def update_media_buy_op(
        self,
        seller_url: str,
        media_buy_id: str | None = None,
        buyer_ref: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Update an existing media buy. Tracked as an operation."""
        seller = self._find_seller_by_url(seller_url)
        if not seller:
            raise ValueError(f"Seller not found: {seller_url}")

        # Track as operation (persisted immediately)
        op = await self.tracker.create(
            operation_type="update_media_buy",
            seller_name=seller.name,
            seller_url=seller.url,
            buyer_ref=buyer_ref or "",
            request_data={"media_buy_id": media_buy_id, **kwargs},
        )

        params: dict[str, Any] = {}
        if media_buy_id:
            params["media_buy_id"] = media_buy_id
        if buyer_ref:
            params["buyer_ref"] = buyer_ref
        for key in ("paused", "end_time", "packages"):
            if key in kwargs and kwargs[key] is not None:
                params[key] = kwargs[key]

        try:
            response = await self._session_call(seller, "update_media_buy", params)
            self.tracker.update_from_response(op.id, response)
            await self.tracker._persist(op)
            return response
        except Exception as e:
            self.tracker.mark_failed(op.id, str(e))
            await self.tracker._persist(op)
            raise

    async def sync_creatives_op(
        self,
        seller_url: str,
        creatives: list[dict],
        media_buy_id: str | None = None,
    ) -> dict[str, Any]:
        """Upload/sync creatives for a media buy. Tracked as an operation."""
        seller = self._find_seller_by_url(seller_url)
        if not seller:
            raise ValueError(f"Seller not found: {seller_url}")

        op = await self.tracker.create(
            operation_type="sync_creatives",
            seller_name=seller.name,
            seller_url=seller.url,
            buyer_ref="",
            request_data={"media_buy_id": media_buy_id, "creative_count": len(creatives)},
        )

        params: dict[str, Any] = {"creatives": creatives}
        if media_buy_id:
            params["media_buy_id"] = media_buy_id

        try:
            response = await self._session_call(seller, "sync_creatives", params)
            self.tracker.update_from_response(op.id, response)
            await self.tracker._persist(op)
            return response
        except Exception as e:
            self.tracker.mark_failed(op.id, str(e))
            await self.tracker._persist(op)
            raise

    async def list_creatives_op(
        self,
        seller_url: str,
        filters: dict | None = None,
    ) -> dict[str, Any]:
        """List creatives from a seller's creative library."""
        seller = self._find_seller_by_url(seller_url)
        if not seller:
            raise ValueError(f"Seller not found: {seller_url}")

        params: dict[str, Any] = {}
        if filters:
            params["filters"] = filters
        return await self._session_call(seller, "list_creatives", params)

    async def provide_feedback_op(
        self,
        seller_url: str,
        media_buy_id: str,
        performance_index: float,
        measurement_period: dict[str, Any],
    ) -> dict[str, Any]:
        """Provide performance feedback to a seller."""
        seller = self._find_seller_by_url(seller_url)
        if not seller:
            raise ValueError(f"Seller not found: {seller_url}")
        return await self._session_call(seller, "provide_performance_feedback", {
            "media_buy_id": media_buy_id,
            "performance_index": performance_index,
            "measurement_period": measurement_period,
        })

    # --- Signals ---

    async def get_signals_op(
        self, seller_url: str, brief: str, platforms: list[dict] | None = None
    ) -> dict[str, Any]:
        """Discover audience signals from a signals agent."""
        seller = self._find_seller_by_url(seller_url)
        if not seller:
            raise ValueError(f"Seller not found: {seller_url}")
        params: dict[str, Any] = {"signal_spec": brief}
        if platforms:
            params["platforms"] = platforms
        return await self._session_call(seller, "get_signals", params)

    async def activate_signal_op(
        self, seller_url: str, signal_id: str, platform: dict[str, Any]
    ) -> dict[str, Any]:
        """Activate a signal for use in campaigns."""
        seller = self._find_seller_by_url(seller_url)
        if not seller:
            raise ValueError(f"Seller not found: {seller_url}")
        return await self._session_call(seller, "activate_signal", {
            "signal_id": signal_id,
            "platform": platform,
        })

    # --- Delivery & Monitoring ---

    async def check_delivery(
        self, seller: SellerAgent, media_buy_id: str
    ) -> dict[str, Any]:
        """Check delivery metrics for a media buy."""
        return await self._session_call(
            seller, "get_media_buy_delivery", {"media_buy_ids": [media_buy_id]}
        )

    # --- Task Management (Standard: tasks/get, tasks/list) ---

    async def poll_pending_operations(self) -> list[dict]:
        """Poll all pending operations for status updates using tasks/get."""
        pending = self.tracker.get_pending()
        results = []

        for op in pending:
            if not op.task_id:
                continue

            seller = self._find_seller_by_url(op.seller_url)
            if not seller:
                continue

            try:
                response = await self._session_call(
                    seller, "tasks/get",
                    {"task_id": op.task_id, "include_result": True},
                )
                op = self.tracker.update_from_response(op.id, response)
                op.poll_count += 1
                await self.tracker._persist(op)
                results.append({"operation_id": op.id, "status": op.status.value})
            except Exception as e:
                logger.warning(f"Failed to poll operation {op.id}: {e}")

        return results

    async def list_seller_tasks(
        self, seller: SellerAgent, statuses: list[str] | None = None
    ) -> dict[str, Any]:
        """List tasks on a specific seller using tasks/list."""
        params: dict[str, Any] = {}
        if statuses:
            params["filters"] = {"statuses": statuses}
        return await self._session_call(seller, "tasks/list", params)

    async def reconcile_state(self, seller: SellerAgent) -> dict[str, Any]:
        """Reconcile local operation state with server state using tasks/list.

        Returns dict with:
        - missing_from_client: task IDs on server not tracked locally
        - missing_from_server: task IDs tracked locally but gone from server
        - total_pending: count of pending tasks
        """
        try:
            remote = await self.list_seller_tasks(
                seller, statuses=["submitted", "working", "input-required"]
            )
        except Exception as e:
            logger.warning(f"Reconciliation failed for {seller.name}: {e}")
            return {"error": str(e)}

        remote_tasks = remote.get("tasks", [])
        server_ids = {t.get("task_id") for t in remote_tasks if t.get("task_id")}

        # Local ops for this seller
        local_ops = [
            op for op in self.tracker.get_pending()
            if op.seller_url.rstrip("/") == seller.url.rstrip("/") and op.task_id
        ]
        client_ids = {op.task_id for op in local_ops}

        return {
            "missing_from_client": list(server_ids - client_ids),
            "missing_from_server": list(client_ids - server_ids),
            "total_pending": len(remote_tasks),
        }

    async def reconcile_all_sellers(self) -> list[dict]:
        """Reconcile state across all sellers that have pending operations."""
        results = []
        sellers_with_ops = set()
        for op in self.tracker.get_pending():
            sellers_with_ops.add(op.seller_url.rstrip("/"))

        for seller in self._sellers:
            if seller.url.rstrip("/") in sellers_with_ops:
                result = await self.reconcile_state(seller)
                results.append({"seller": seller.name, **result})

        return results

    # --- Human-in-the-Loop ---

    async def provide_input(self, operation_id: str, input_data: str | dict) -> dict[str, Any]:
        """Provide human input for an input-required operation.

        Uses the seller session to maintain context_id across the conversation.
        """
        op = self.tracker.get(operation_id)
        if not op:
            raise ValueError(f"Operation not found: {operation_id}")
        if op.status != TaskStatus.INPUT_REQUIRED:
            raise ValueError(
                f"Operation {operation_id} is not input-required (status: {op.status.value})"
            )

        seller = self._find_seller_by_url(op.seller_url)
        if not seller:
            raise ValueError(f"Seller not found for operation: {operation_id}")

        # Build params for resuming the operation
        params: dict[str, Any] = {}
        if op.context_id:
            params["context_id"] = op.context_id
        if isinstance(input_data, str):
            params["additional_info"] = input_data
        else:
            params.update(input_data)

        try:
            response = await self._session_call(seller, op.operation_type, params)
            self.tracker.update_from_response(op.id, response)
            await self.tracker._persist(op)
            return response
        except Exception as e:
            self.tracker.mark_failed(op.id, f"Input submission failed: {e}")
            await self.tracker._persist(op)
            raise
