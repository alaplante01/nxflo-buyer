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

from src.accounts.manager import AccountError, AccountManager, SellerAccount
from src.authorization.adagents import SalesAgentAuthorizer, SellerNotAuthorizedError
from src.brand.resolver import (
    BrandAuthorizationError,
    BrandAuthorizationResolver,
    build_brand_ref,
    domain_from_url,
)
from src.buying.requests import (
    RequestBuildError,
    build_create_media_buy_request,
    build_get_products_request,
    build_package,
    format_selector,
    new_idempotency_key,
    with_version,
)
from src.buying.tracker import OperationTracker, TaskStatus, TrackedOperation
from src.config import settings
from src.connections.errors import SellerError
from src.connections.session import SellerSession
from src.discovery.registry import SellerAgent, discover_all_sellers

# Failures that end a buy attempt with a recorded, human-readable reason.
BUY_FAILURES = (
    BrandAuthorizationError,
    SellerNotAuthorizedError,
    AccountError,
    RequestBuildError,
    SellerError,
)

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

    def __init__(self) -> None:
        self.tracker = OperationTracker()
        self._sellers: list[SellerAgent] = []
        self._seller_sessions: dict[str, SellerSession] = {}
        self._last_discovery: float = 0
        self.accounts = AccountManager(call_tool=self._session_call)
        self.brands = BrandAuthorizationResolver()
        self.sales_authorizer = SalesAgentAuthorizer()
        # (seller_url, product_id) -> raw product from the latest get_products
        self._products: dict[tuple[str, str], dict[str, Any]] = {}

    @staticmethod
    def brand_ref(
        brand_domain: str | None = None,
        brand_id: str | None = None,
        countries: list[str] | None = None,
        brand_url: str | None = None,
    ) -> dict[str, Any]:
        """AdCP BrandRef for the advertiser (domain falls back to the brand URL)."""
        domain = brand_domain or domain_from_url(brand_url or settings.brand_url)
        return build_brand_ref(domain, brand_id, countries)

    def get_seller_session(self, seller: SellerAgent) -> SellerSession:
        """Get or create a session for a seller (maintains context_id)."""
        key = seller.url.rstrip("/")
        if key not in self._seller_sessions:
            self._seller_sessions[key] = SellerSession(seller)
        return self._seller_sessions[key]

    async def _session_call(
        self, seller: SellerAgent, tool: str, params: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
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
        self,
        brief: str,
        brand_name: str | None = None,
        brand_url: str | None = None,
        *,
        brand_domain: str | None = None,
        brand_id: str | None = None,
        countries: list[str] | None = None,
    ) -> list[SellerProduct]:
        """Fan out get_products to all sales-capable sellers, collect and normalize results."""
        if not self._sellers:
            await self.discover_sellers()

        brand = self.brand_ref(brand_domain, brand_id, countries, brand_url)
        logger.debug("Product search for %s (%s)", brand_name or settings.brand_name, brand["domain"])

        # Only query sellers that have get_products
        eligible = [s for s in self._sellers if "get_products" in s.tools]
        if not eligible:
            logger.warning("No sellers with get_products capability found")
            eligible = self._sellers  # Fallback: try all

        results = await asyncio.gather(
            *[self._get_products_from_seller(seller, brief, brand) for seller in eligible],
            return_exceptions=True,
        )

        # Collect all products
        all_products: list[SellerProduct] = []
        for seller, result in zip(eligible, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning(f"Failed to get products from {seller.name}: {result}")
                continue
            all_products.extend(result)

        logger.info(f"Found {len(all_products)} products across {len(eligible)} sellers")
        return all_products

    async def _get_products_from_seller(
        self, seller: SellerAgent, brief: str, brand: dict[str, Any]
    ) -> list[SellerProduct]:
        """Get products from a single seller and normalize."""
        try:
            account = await self.accounts.discovery_account_ref(seller, brand)
            response = await self._session_call(
                seller, "get_products", build_get_products_request(brief, brand, account)
            )
        except (SellerError, AccountError) as e:
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

            if p.get("product_id"):
                self._products[(seller.url.rstrip("/"), str(p["product_id"]))] = p
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

    def _known_product(self, seller: SellerAgent, product_id: str) -> dict[str, Any] | None:
        return self._products.get((seller.url.rstrip("/"), product_id))

    async def _authorize_seller(
        self, seller: SellerAgent, product_ids: list[str], countries: list[str] | None
    ) -> None:
        """adagents.json check for every product in the buy (fail closed)."""
        if settings.skip_sales_agent_authorization_check:
            logger.warning(
                "Sales-agent authorization check SKIPPED for %s "
                "(NXFLO_SKIP_SALES_AGENT_AUTHORIZATION_CHECK=true; sandbox use only)",
                seller.name,
            )
            return
        for product_id in product_ids:
            raw = self._known_product(seller, product_id)
            if raw is None:
                raise SellerNotAuthorizedError(
                    seller.url, None, f"product {product_id} was not discovered; cannot verify publisher authorization"
                )
            await self.sales_authorizer.verify_product(seller.url, raw, countries=countries)

    def _package_selector(self, product: SellerProduct) -> dict[str, Any]:
        selector = format_selector(product.raw)
        if not selector and product.format_ids and not product.raw.get("format_options"):
            selector = {"format_ids": product.format_ids}
        return selector

    def _check_seller_optimized(self, seller: SellerAgent, allocation: dict[str, Any] | None) -> None:
        if not allocation or allocation.get("mode") != "seller_optimized":
            return
        media_buy = seller.capabilities.get("media_buy")
        features = media_buy.get("features") if isinstance(media_buy, dict) else None
        if not isinstance(features, dict) or features.get("seller_optimized_budget") is not True:
            raise RequestBuildError(f"{seller.name} does not advertise media_buy.features.seller_optimized_budget")

    async def _fail_op(self, op: TrackedOperation, error: Exception) -> None:
        self.tracker.mark_failed(op.id, str(error))
        await self.tracker._persist(op)

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
        *,
        brand_domain: str | None = None,
        brand_id: str | None = None,
        brand_countries: list[str] | None = None,
        budget_allocation: dict[str, Any] | None = None,
    ) -> BuyResult:
        """Execute a media buy on a specific product (AdCP 3.2.1 create_media_buy).

        Before the seller is called:
        1. brand.json authorized_operators must list our operator domain (fail closed);
        2. the seller must be authorized in each publisher's adagents.json (fail closed,
           skippable only via NXFLO_SKIP_SALES_AGENT_AUTHORIZATION_CHECK);
        3. an account reference is resolved (natural key or account_id per seller mode).

        Supports single product, custom multi-package, proposal execution, targeting
        overlays, pacing, and an explicit budget_allocation ({"mode": "fixed"} unless a
        seller_optimized block is supplied).
        """
        seller = product.seller
        ref = buyer_ref or f"nxflo-{uuid.uuid4().hex[:12]}"
        idempotency_key = new_idempotency_key()
        brand = self.brand_ref(brand_domain, brand_id, brand_countries)

        if not end_time:
            end_time = (datetime.now(UTC) + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Track the operation (persisted immediately for crash recovery)
        op = await self.tracker.create(
            operation_type="create_media_buy",
            seller_name=seller.name,
            seller_url=seller.url,
            buyer_ref=ref,
            request_data={
                "product_id": product.product_id,
                "budget": budget,
                "brand": brand,
                "proposal_id": proposal_id,
                "idempotency_key": idempotency_key,
            },
        )

        try:
            params = await self._prepare_create_media_buy(
                op, product, budget, ref, idempotency_key, brand, end_time,
                packages=packages, targeting=targeting, pacing=pacing,
                proposal_id=proposal_id, currency=currency, budget_allocation=budget_allocation,
            )
            response = await self.get_seller_session(seller).call_with_retry("create_media_buy", params)
        except BUY_FAILURES as e:
            await self._fail_op(op, e)
            return BuyResult(operation_id=op.id, seller_name=seller.name, status="failed", error=str(e))

        op = self.tracker.update_from_response(op.id, response)
        await self.tracker._persist(op)
        return BuyResult(
            operation_id=op.id,
            seller_name=seller.name,
            status=op.status.value,
            media_buy_id=op.media_buy_id,
            task_id=op.task_id,
            raw_response=response,
        )

    async def _prepare_create_media_buy(
        self,
        op: TrackedOperation,
        product: SellerProduct,
        budget: float,
        ref: str,
        idempotency_key: str,
        brand: dict[str, Any],
        end_time: str,
        *,
        packages: list[dict] | None,
        targeting: dict | None,
        pacing: str | None,
        proposal_id: str | None,
        currency: str,
        budget_allocation: dict[str, Any] | None,
    ) -> dict[str, Any]:
        seller = product.seller
        countries = _target_countries(targeting)
        await self.brands.verify_operator(brand, settings.operator_domain)

        if packages:
            for pkg in packages:
                pkg.pop("buyer_ref", None)  # 3.2: correlation lives in package context
                pkg.setdefault("context", {}).setdefault("buyer_ref", ref)
            product_ids = [str(pkg.get("product_id")) for pkg in packages]
        else:
            product_ids = [product.product_id]
        await self._authorize_seller(seller, product_ids, countries)

        account = await self.accounts.resolve_account_ref(seller, brand, task="create_media_buy")
        self._check_seller_optimized(seller, budget_allocation)
        seller_optimized = bool(budget_allocation and budget_allocation.get("mode") == "seller_optimized")
        total_budget = {"amount": budget, "currency": currency} if proposal_id or seller_optimized else None

        if proposal_id:
            built_packages = None
        elif packages:
            built_packages = packages
        else:
            built_packages = [
                build_package(
                    product_id=product.product_id,
                    pricing_option_id=product.pricing_option_id or "cpm-standard",
                    budget=None if seller_optimized else budget,
                    buyer_ref=ref,
                    selector=self._package_selector(product),
                    targeting=targeting,
                    pacing=pacing,
                )
            ]

        push_config = None
        reporting_webhook = None
        if settings.webhook_base_url:
            from src.webhooks.config import build_push_notification_config, build_reporting_webhook

            push_config = build_push_notification_config(task_type="create_media_buy", operation_id=op.id)
            op.webhook_config = push_config
            reporting_webhook = build_reporting_webhook(operation_id=op.id)

        params = build_create_media_buy_request(
            idempotency_key=idempotency_key,
            account=account,
            brand=brand,
            start_time="asap",
            end_time=end_time,
            packages=built_packages,
            proposal_id=proposal_id,
            total_budget=total_budget,
            budget_allocation=budget_allocation,
            context={"buyer_ref": ref},
            push_notification_config=push_config,
            reporting_webhook=reporting_webhook,
        )
        op.request_data.update(
            {"account": account, "budget_allocation": params.get("budget_allocation")}
        )
        await self.tracker._persist(op)
        return params

    def _tracked_buy(self, seller: SellerAgent, media_buy_id: str | None, buyer_ref: str | None) -> TrackedOperation | None:
        for op in self.tracker.list_all():
            if op.operation_type != "create_media_buy" or op.seller_url.rstrip("/") != seller.url.rstrip("/"):
                continue
            if (media_buy_id and op.media_buy_id == media_buy_id) or (buyer_ref and op.buyer_ref == buyer_ref):
                return op
        return None

    async def _account_for_existing_buy(
        self, seller: SellerAgent, media_buy_id: str | None, buyer_ref: str | None, task: str
    ) -> tuple[dict[str, Any], str | None]:
        """Reuse the account a buy was created under; else resolve for the default brand."""
        tracked = self._tracked_buy(seller, media_buy_id, buyer_ref)
        if tracked is not None and isinstance(tracked.request_data.get("account"), dict):
            return tracked.request_data["account"], media_buy_id or tracked.media_buy_id
        account = await self.accounts.resolve_account_ref(seller, self.brand_ref(), task=task, provision=False)
        return account, media_buy_id or (tracked.media_buy_id if tracked else None)

    async def update_media_buy_op(
        self,
        seller_url: str,
        media_buy_id: str | None = None,
        buyer_ref: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Update an existing media buy (3.2: media_buy_id + account + idempotency_key)."""
        seller = self._find_seller_by_url(seller_url)
        if not seller:
            raise ValueError(f"Seller not found: {seller_url}")
        account, resolved_id = await self._account_for_existing_buy(
            seller, media_buy_id, buyer_ref, "update_media_buy"
        )
        if not resolved_id:
            raise ValueError("media_buy_id is required (no tracked buy matches buyer_ref)")

        params: dict[str, Any] = {
            "idempotency_key": new_idempotency_key(),
            "account": account,
            "media_buy_id": resolved_id,
        }
        for key in ("paused", "end_time", "packages"):
            if key in kwargs and kwargs[key] is not None:
                params[key] = kwargs[key]

        # Track as operation (persisted immediately)
        op = await self.tracker.create(
            operation_type="update_media_buy",
            seller_name=seller.name,
            seller_url=seller.url,
            buyer_ref=buyer_ref or "",
            request_data={"media_buy_id": resolved_id, "account": account,
                          "idempotency_key": params["idempotency_key"], **kwargs},
        )
        try:
            response = await self._session_call(seller, "update_media_buy", with_version(params))
        except SellerError as e:
            await self._fail_op(op, e)
            raise
        self.tracker.update_from_response(op.id, response)
        await self.tracker._persist(op)
        return response

    async def sync_creatives_op(
        self,
        seller_url: str,
        creatives: list[dict],
        media_buy_id: str | None = None,
    ) -> dict[str, Any]:
        """Upload/sync creatives (3.2: account + idempotency_key). Tracked as an operation."""
        seller = self._find_seller_by_url(seller_url)
        if not seller:
            raise ValueError(f"Seller not found: {seller_url}")
        account, _ = await self._account_for_existing_buy(seller, media_buy_id, None, "sync_creatives")

        params: dict[str, Any] = {
            "idempotency_key": new_idempotency_key(),
            "account": account,
            "creatives": creatives,
        }
        if media_buy_id:
            params["media_buy_id"] = media_buy_id  # pre-3.2 sellers; ignored by 3.2 schemas

        op = await self.tracker.create(
            operation_type="sync_creatives",
            seller_name=seller.name,
            seller_url=seller.url,
            buyer_ref="",
            request_data={"media_buy_id": media_buy_id, "creative_count": len(creatives),
                          "account": account, "idempotency_key": params["idempotency_key"]},
        )
        try:
            response = await self._session_call(seller, "sync_creatives", with_version(params))
        except SellerError as e:
            await self._fail_op(op, e)
            raise
        self.tracker.update_from_response(op.id, response)
        await self.tracker._persist(op)
        return response

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
            "idempotency_key": new_idempotency_key(),
            "media_buy_id": media_buy_id,
            "performance_index": performance_index,
            "measurement_period": measurement_period,
        })

    # --- Accounts (AdCP 3.2) ---

    async def list_accounts_op(
        self, seller_url: str, status: str | None = None, sandbox: bool | None = None
    ) -> list[SellerAccount]:
        """Call list_accounts on a seller and persist the results."""
        seller = self._find_seller_by_url(seller_url)
        if not seller:
            raise ValueError(f"Seller not found: {seller_url}")
        return await self.accounts.list_accounts(seller, status=status, sandbox=sandbox)

    async def sync_accounts_op(
        self,
        seller_url: str,
        *,
        brand_domain: str | None = None,
        brand_id: str | None = None,
        countries: list[str] | None = None,
        billing: str | None = None,
        dry_run: bool = False,
    ) -> list[SellerAccount]:
        """Provision a buyer-declared account after verifying brand.json authorization."""
        seller = self._find_seller_by_url(seller_url)
        if not seller:
            raise ValueError(f"Seller not found: {seller_url}")
        brand = self.brand_ref(brand_domain, brand_id, countries)
        await self.brands.verify_operator(brand, settings.operator_domain)
        return await self.accounts.sync_accounts(seller, brand, billing=billing, dry_run=dry_run)

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
                logger.warning(f"Failed to poll operation {op.id}: {e}", exc_info=True)

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
            logger.warning(f"Reconciliation failed for {seller.name}: {e}", exc_info=True)
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


def _target_countries(targeting: dict | None) -> list[str] | None:
    """Countries a buy targets (targeting_overlay.geo_countries), if any."""
    if not isinstance(targeting, dict):
        return None
    countries = targeting.get("geo_countries")
    if isinstance(countries, list) and countries:
        return [str(c).upper() for c in countries]
    return None
