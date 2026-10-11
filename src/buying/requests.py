"""Pure builders for AdCP 3.2.1 request payloads (no I/O; unit-testable).

Schemas: /schemas/3.2.1/media-buy/create-media-buy-request.json (idempotency_key,
account, brand, start_time, end_time required; budget_allocation sent explicitly),
/schemas/3.2.1/media-buy/get-products-request.json (buying_mode required; brand is a
BrandRef; account only once provisioned).
"""

import uuid
from typing import Any

from src.config import settings

FIXED_ALLOCATION: dict[str, str] = {"mode": "fixed"}


class RequestBuildError(ValueError):
    """Inputs cannot form a schema-valid AdCP request."""


def new_idempotency_key(prefix: str = "nxflo") -> str:
    """Fresh key matching ^[A-Za-z0-9_.:-]{16,255}$ (unique per seller request)."""
    return f"{prefix}-{uuid.uuid4().hex}"


def with_version(params: dict[str, Any]) -> dict[str, Any]:
    """Add release-precision adcp_version plus the deprecated major for 3.x sellers."""
    versioned = dict(params)
    versioned.setdefault("adcp_version", settings.adcp_version)
    versioned.setdefault("adcp_major_version", int(settings.adcp_version.split(".", 1)[0]))
    return versioned


def build_get_products_request(
    brief: str, brand: dict[str, Any], account: dict[str, Any] | None = None
) -> dict[str, Any]:
    params: dict[str, Any] = {"buying_mode": "brief", "brief": brief, "brand": brand}
    if account:
        params["account"] = account
    return with_version(params)


def format_selector(product: dict[str, Any]) -> dict[str, Any]:
    """Pick exactly one format route for a package from a product.

    3.2 products carry format_options[]: reference them with format_option_refs
    (publisher- or product-scoped). A single ID-less declaration uses the direct
    canonical selector (format_kind + params). Legacy products without
    format_options fall back to deprecated format_ids for older 3.x sellers.
    """
    options = [o for o in product.get("format_options") or [] if isinstance(o, dict)]
    refs: list[dict[str, Any]] = []
    for option in options:
        option_id = option.get("format_option_id")
        if not option_id:
            continue
        if option.get("publisher_domain"):
            refs.append(
                {
                    "scope": "publisher",
                    "publisher_domain": option["publisher_domain"],
                    "format_option_id": option_id,
                }
            )
        else:
            refs.append({"scope": "product", "format_option_id": option_id})
    if refs:
        return {"format_option_refs": refs}
    if len(options) == 1 and options[0].get("format_kind"):
        selector: dict[str, Any] = {"format_kind": options[0]["format_kind"]}
        if isinstance(options[0].get("params"), dict):
            selector["params"] = options[0]["params"]
        return selector
    legacy = product.get("format_ids")
    if not options and isinstance(legacy, list) and legacy:
        return {"format_ids": legacy}
    return {}


def build_package(
    *,
    product_id: str,
    pricing_option_id: str,
    budget: float | None,
    buyer_ref: str,
    selector: dict[str, Any] | None = None,
    targeting: dict[str, Any] | None = None,
    pacing: str | None = None,
) -> dict[str, Any]:
    pkg: dict[str, Any] = {
        "product_id": product_id,
        "pricing_option_id": pricing_option_id,
        "context": {"buyer_ref": buyer_ref},
    }
    if budget is not None:
        pkg["budget"] = budget
    pkg.update(selector or {})
    if targeting:
        pkg["targeting_overlay"] = targeting
    if pacing:
        pkg["pacing"] = pacing
    return pkg


def resolve_budget_allocation(
    configured: dict[str, Any] | None, *, proposal: bool
) -> dict[str, Any] | None:
    """Explicit {"mode": "fixed"} unless a seller_optimized block is configured.

    Proposal execution must omit budget_allocation (the committed proposal supplies it).
    """
    if proposal:
        if configured:
            raise RequestBuildError("budget_allocation must be omitted when executing a proposal")
        return None
    if not configured or configured.get("mode") == "fixed":
        return dict(FIXED_ALLOCATION)
    if configured.get("mode") != "seller_optimized":
        raise RequestBuildError(f"unknown budget_allocation mode {configured.get('mode')!r}")
    goals = configured.get("optimization_goals")
    if not isinstance(goals, list) or not goals:
        raise RequestBuildError("seller_optimized budget_allocation needs optimization_goals")
    return {"mode": "seller_optimized", "optimization_goals": goals}


def build_create_media_buy_request(
    *,
    idempotency_key: str,
    account: dict[str, Any],
    brand: dict[str, Any],
    start_time: str,
    end_time: str,
    packages: list[dict[str, Any]] | None = None,
    proposal_id: str | None = None,
    total_budget: dict[str, Any] | None = None,
    budget_allocation: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
    push_notification_config: dict[str, Any] | None = None,
    reporting_webhook: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not account:
        raise RequestBuildError("create_media_buy requires an account reference")
    if not brand or not brand.get("domain"):
        raise RequestBuildError("create_media_buy requires a brand reference")
    params: dict[str, Any] = {
        "idempotency_key": idempotency_key,
        "account": account,
        "brand": brand,
        "start_time": start_time,
        "end_time": end_time,
    }
    allocation = resolve_budget_allocation(budget_allocation, proposal=bool(proposal_id))
    if proposal_id:
        if not total_budget:
            raise RequestBuildError("proposal execution requires total_budget")
        params["proposal_id"] = proposal_id
        params["total_budget"] = total_budget
    else:
        if not packages:
            raise RequestBuildError("create_media_buy requires packages or a proposal_id")
        if allocation and allocation["mode"] == "fixed":
            missing = [p.get("product_id") for p in packages if p.get("budget") is None]
            if missing:
                raise RequestBuildError(
                    f"fixed allocation needs a budget on every package: {missing}"
                )
        if allocation and allocation["mode"] == "seller_optimized" and not total_budget:
            raise RequestBuildError("seller_optimized allocation requires total_budget")
        params["packages"] = packages
        params["budget_allocation"] = allocation
        if total_budget:
            params["total_budget"] = total_budget
    if context:
        params["context"] = context
    if push_notification_config:
        params["push_notification_config"] = push_notification_config
    if reporting_webhook:
        params["reporting_webhook"] = reporting_webhook
    return with_version(params)
