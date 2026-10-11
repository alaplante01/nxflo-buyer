"""create_media_buy / get_products payloads (3.2.1) and the fail-closed buy flow."""

from typing import Any

import pytest

from src.accounts.manager import AccountError
from src.authorization.adagents import SellerNotAuthorizedError
from src.brand.resolver import BrandAuthorizationError, BrandResolution
from src.buying import requests as req
from src.buying.orchestrator import BuyingOrchestrator, SellerProduct
from src.config import settings
from src.discovery.registry import SellerAgent

PRODUCT_RAW = {
    "product_id": "prod_1",
    "name": "Display",
    "publisher_properties": [{"publisher_domain": "news.example", "selection_type": "all"}],
    "format_options": [{"format_option_id": "display_300x250", "format_kind": "image", "params": {"width": 300, "height": 250}}],
    "pricing_options": [{"pricing_option_id": "cpm_usd", "pricing_model": "cpm", "rate": 5.0}],
}


# --- pure builders ---


def test_explicit_fixed_budget_allocation() -> None:
    params = req.build_create_media_buy_request(
        idempotency_key="nxflo-0123456789abcdef", account={"account_id": "a"}, brand={"domain": "acme.example"},
        start_time="asap", end_time="2026-12-01T00:00:00Z",
        packages=[{"product_id": "p", "pricing_option_id": "o", "budget": 10}],
    )
    assert params["budget_allocation"] == {"mode": "fixed"}
    assert params["adcp_version"] == "3.2" and params["adcp_major_version"] == 3
    assert "total_budget" not in params and "brand_manifest" not in params and "buyer_ref" not in params


def test_seller_optimized_and_proposal_allocation_rules() -> None:
    goals = [{"kind": "metric", "metric": "clicks"}]
    common = {"idempotency_key": "nxflo-0123456789abcdef", "account": {"account_id": "a"},
              "brand": {"domain": "acme.example"}, "start_time": "asap", "end_time": "2026-12-01T00:00:00Z"}
    packages = [{"product_id": "p", "pricing_option_id": "o"}]
    optimized = req.build_create_media_buy_request(
        **common, packages=packages, total_budget={"amount": 100, "currency": "USD"},
        budget_allocation={"mode": "seller_optimized", "optimization_goals": goals},
    )
    assert optimized["budget_allocation"] == {"mode": "seller_optimized", "optimization_goals": goals}
    with pytest.raises(req.RequestBuildError):
        req.build_create_media_buy_request(**common, packages=packages,
                                           budget_allocation={"mode": "seller_optimized", "optimization_goals": goals})
    with pytest.raises(req.RequestBuildError):
        req.build_create_media_buy_request(**common, packages=packages)  # fixed mode needs package budgets
    proposal = req.build_create_media_buy_request(**common, proposal_id="prop_1", total_budget={"amount": 1, "currency": "USD"})
    assert "budget_allocation" not in proposal and proposal["proposal_id"] == "prop_1"
    with pytest.raises(req.RequestBuildError):
        req.build_create_media_buy_request(**common, proposal_id="prop_1", total_budget={"amount": 1, "currency": "USD"},
                                           budget_allocation={"mode": "fixed"})


def test_requires_account_and_brand() -> None:
    with pytest.raises(req.RequestBuildError):
        req.build_create_media_buy_request(idempotency_key="nxflo-0123456789abcdef", account={}, brand={"domain": "a.example"},
                                           start_time="asap", end_time="x", packages=[{"budget": 1}])
    with pytest.raises(req.RequestBuildError):
        req.build_create_media_buy_request(idempotency_key="nxflo-0123456789abcdef", account={"account_id": "a"}, brand={},
                                           start_time="asap", end_time="x", packages=[{"budget": 1}])


def test_format_selector_routes() -> None:
    assert req.format_selector(PRODUCT_RAW) == {"format_option_refs": [{"scope": "product", "format_option_id": "display_300x250"}]}
    publisher = {"format_options": [{"format_option_id": "f1", "publisher_domain": "news.example", "format_kind": "image"}]}
    assert req.format_selector(publisher)["format_option_refs"][0] == {"scope": "publisher", "publisher_domain": "news.example", "format_option_id": "f1"}
    direct = {"format_options": [{"format_kind": "video", "params": {"duration_s": 15}}]}
    assert req.format_selector(direct) == {"format_kind": "video", "params": {"duration_s": 15}}
    legacy = {"format_ids": [{"agent_url": "https://c.example", "id": "d300"}]}
    assert req.format_selector(legacy) == {"format_ids": legacy["format_ids"]}


def test_get_products_request_shape() -> None:
    params = req.build_get_products_request("brief", {"domain": "acme.example"})
    assert params["buying_mode"] == "brief" and params["brand"] == {"domain": "acme.example"}
    assert "account" not in params and "brand_manifest" not in params
    assert req.build_get_products_request("b", {"domain": "a.example"}, {"account_id": "x"})["account"] == {"account_id": "x"}


def test_idempotency_key_pattern() -> None:
    import re

    assert re.fullmatch(r"[A-Za-z0-9_.:-]{16,255}", req.new_idempotency_key())


# --- orchestrator buy flow ---


class Calls:
    def __init__(self) -> None:
        self.seller: list[tuple[str, dict[str, Any]]] = []


def make_orchestrator(monkeypatch: pytest.MonkeyPatch, *, brand_ok: bool = True, seller_ok: bool = True,
                      account: dict[str, Any] | Exception | None = None) -> tuple[BuyingOrchestrator, SellerProduct, Calls]:
    orch = BuyingOrchestrator()
    seller = SellerAgent(name="S", url="https://sales.example/mcp", tools=["create_media_buy"],
                         capabilities={"media_buy": {"features": {"seller_optimized_budget": True}}})
    orch._sellers = [seller]
    orch._products[("https://sales.example/mcp", "prod_1")] = PRODUCT_RAW
    calls = Calls()

    async def verify_operator(brand: dict[str, Any], operator: str, **_: Any) -> BrandResolution:
        if not brand_ok:
            raise BrandAuthorizationError("operator_not_authorized", brand["domain"], "nope")
        return BrandResolution(domain=brand["domain"], kind="house_portfolio", brand_id=None, house_domain=None)

    async def verify_product(seller_url: str, product: dict[str, Any], **_: Any) -> None:
        if not seller_ok:
            raise SellerNotAuthorizedError(seller_url, "news.example", "not listed")

    async def resolve_account_ref(*_: Any, **__: Any) -> dict[str, Any]:
        if isinstance(account, Exception):
            raise account
        return account or {"brand": {"domain": "acme.example"}, "operator": "nxflo.io"}

    async def seller_call(tool: str, params: dict[str, Any], **_: Any) -> dict[str, Any]:
        calls.seller.append((tool, params))
        return {"status": "completed", "media_buy_id": "mb_1"}

    monkeypatch.setattr(orch.brands, "verify_operator", verify_operator)
    monkeypatch.setattr(orch.sales_authorizer, "verify_product", verify_product)
    monkeypatch.setattr(orch.accounts, "resolve_account_ref", resolve_account_ref)
    monkeypatch.setattr(orch.get_seller_session(seller), "call_with_retry", seller_call)
    product = SellerProduct(seller=seller, product_id="prod_1", name="Display", pricing_option_id="cpm_usd", raw=PRODUCT_RAW)
    return orch, product, calls


async def test_buy_sends_3_2_create_media_buy(monkeypatch: pytest.MonkeyPatch) -> None:
    orch, product, calls = make_orchestrator(monkeypatch)
    result = await orch.buy(product, 500.0, buyer_ref="ref-1", brand_domain="acme.example")
    assert result.status == "completed" and result.media_buy_id == "mb_1"
    tool, params = calls.seller[0]
    assert tool == "create_media_buy"
    assert params["account"] == {"brand": {"domain": "acme.example"}, "operator": "nxflo.io"}
    assert params["brand"] == {"domain": "acme.example"}
    assert params["budget_allocation"] == {"mode": "fixed"}
    assert len(params["idempotency_key"]) >= 16 and params["context"] == {"buyer_ref": "ref-1"}
    [pkg] = params["packages"]
    assert pkg["budget"] == 500.0 and pkg["pricing_option_id"] == "cpm_usd"
    assert pkg["format_option_refs"] == [{"scope": "product", "format_option_id": "display_300x250"}]
    assert pkg["context"] == {"buyer_ref": "ref-1"} and "format_ids" not in pkg
    op = orch.tracker.get(result.operation_id)
    assert op.request_data["account"] == params["account"]
    assert op.request_data["idempotency_key"] == params["idempotency_key"]


async def test_seller_optimized_buy(monkeypatch: pytest.MonkeyPatch) -> None:
    orch, product, calls = make_orchestrator(monkeypatch)
    allocation = {"mode": "seller_optimized", "optimization_goals": [{"kind": "metric", "metric": "clicks"}]}
    await orch.buy(product, 1000.0, currency="EUR", budget_allocation=allocation)
    _, params = calls.seller[0]
    assert params["budget_allocation"]["mode"] == "seller_optimized"
    assert params["total_budget"] == {"amount": 1000.0, "currency": "EUR"}
    assert "budget" not in params["packages"][0]

    product.seller.capabilities = {}
    failed = await orch.buy(product, 1000.0, budget_allocation=allocation)
    assert failed.status == "failed" and "seller_optimized_budget" in failed.error


@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [
        ({"brand_ok": False}, "operator_not_authorized"),
        ({"seller_ok": False}, "not authorized"),
        ({"account": AccountError("account_not_usable", "S", "pending_approval")}, "account_not_usable"),
    ],
)
async def test_buy_fails_closed_without_calling_seller(monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, Any], fragment: str) -> None:
    orch, product, calls = make_orchestrator(monkeypatch, **kwargs)
    result = await orch.buy(product, 100.0, brand_domain="acme.example")
    assert result.status == "failed" and fragment in result.error
    assert calls.seller == []
    assert orch.tracker.get(result.operation_id).status.value == "failed"


async def test_unknown_product_fails_adagents_check_unless_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    orch, product, calls = make_orchestrator(monkeypatch)
    orch._products.clear()
    result = await orch.buy(product, 100.0)
    assert result.status == "failed" and "not discovered" in result.error and calls.seller == []

    monkeypatch.setattr(settings, "skip_sales_agent_authorization_check", True)
    result = await orch.buy(product, 100.0)
    assert result.status == "completed" and len(calls.seller) == 1


async def test_proposal_buy_omits_allocation(monkeypatch: pytest.MonkeyPatch) -> None:
    orch, product, calls = make_orchestrator(monkeypatch)
    await orch.buy(product, 250.0, proposal_id="prop_9")
    _, params = calls.seller[0]
    assert "budget_allocation" not in params and "packages" not in params
    assert params["proposal_id"] == "prop_9" and params["total_budget"] == {"amount": 250.0, "currency": "USD"}


async def test_update_media_buy_reuses_account_and_adds_idempotency(monkeypatch: pytest.MonkeyPatch) -> None:
    orch, product, _ = make_orchestrator(monkeypatch)
    result = await orch.buy(product, 100.0, buyer_ref="ref-9", brand_domain="acme.example")
    sent: list[tuple[str, dict[str, Any]]] = []

    async def session_call(seller: SellerAgent, tool: str, params: dict[str, Any], **_: Any) -> dict[str, Any]:
        sent.append((tool, params))
        return {"status": "completed", "media_buy_id": "mb_1"}

    monkeypatch.setattr(orch, "_session_call", session_call)
    await orch.update_media_buy_op("https://sales.example/mcp", buyer_ref="ref-9", paused=True)
    tool, params = sent[0]
    assert tool == "update_media_buy" and params["media_buy_id"] == result.media_buy_id
    assert params["account"] == {"brand": {"domain": "acme.example"}, "operator": "nxflo.io"}
    assert params["paused"] is True and len(params["idempotency_key"]) >= 16

    await orch.sync_creatives_op("https://sales.example/mcp", [{"creative_id": "c1"}], media_buy_id="mb_1")
    tool, params = sent[1]
    assert tool == "sync_creatives" and params["account"]["operator"] == "nxflo.io" and params["idempotency_key"]


async def test_get_products_sends_brand_ref_and_buying_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    orch, _, _ = make_orchestrator(monkeypatch)
    orch._sellers[0].tools = ["get_products"]
    sent: list[dict[str, Any]] = []

    async def session_call(seller: SellerAgent, tool: str, params: dict[str, Any], **_: Any) -> dict[str, Any]:
        sent.append(params)
        return {"products": [PRODUCT_RAW]}

    async def no_account(*_: Any) -> None:
        return None

    monkeypatch.setattr(orch, "_session_call", session_call)
    monkeypatch.setattr(orch.accounts, "discovery_account_ref", no_account)
    products = await orch.get_products_from_all("CTV in the US", brand_domain="acme.example", countries=["us"])
    assert sent[0]["buying_mode"] == "brief" and sent[0]["brand"] == {"domain": "acme.example", "countries": ["US"]}
    assert "account" not in sent[0] and "brand_manifest" not in sent[0]
    assert products[0].pricing_option_id == "cpm_usd"
