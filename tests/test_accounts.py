"""AdCP 3.2 accounts: list_accounts / sync_accounts, both seller modes, persistence."""

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.accounts.manager import AccountError, AccountManager
from src.accounts.mode import AccountMode, determine_account_mode
from src.config import settings
from src.connections.seller import _build_headers
from src.discovery.registry import SellerAgent

BRAND = {"domain": "acme.example"}
URL = "https://seller.example/mcp"
SLUG = "SELLER_EXAMPLE"


class FakeSeller:
    """Records tool calls and replies from a script keyed by tool name."""

    def __init__(self, replies: dict[str, list[dict[str, Any]]]) -> None:
        self.replies = replies
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, seller: SellerAgent, tool: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((tool, params))
        return self.replies[tool].pop(0)


def buyer_declared(**account_caps: Any) -> SellerAgent:
    caps = {"account": {"require_operator_auth": False, "supported_billing": ["operator", "agent"], **account_caps}}
    agent = SellerAgent(name="Seller", url=URL, tools=["list_accounts", "sync_accounts"], capabilities=caps)
    agent.apply_account_config()
    return agent


def operator_authorized(monkeypatch: pytest.MonkeyPatch, token: str | None = "op-credential") -> SellerAgent:
    if token:
        monkeypatch.setenv(f"NXFLO_OPERATOR_TOKEN_{SLUG}", token)
    caps = {"account": {"require_operator_auth": True, "supported_billing": ["operator"]}}
    agent = SellerAgent(name="Social", url=URL, token="agent-credential", tools=["list_accounts"], capabilities=caps)
    agent.apply_account_config()
    return agent


def synced(status: str, **extra: Any) -> dict[str, Any]:
    return {"accounts": [{"brand": BRAND, "operator": "nxflo.io", "action": "created", "status": status,
                          "billing": "operator", "account_id": "acc_1", **extra}]}


def test_mode_from_capabilities_and_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    assert determine_account_mode(URL, {"account": {"require_operator_auth": True}}) == AccountMode.OPERATOR_AUTHORIZED
    assert determine_account_mode(URL, {"account": {}}) == AccountMode.BUYER_DECLARED
    assert determine_account_mode(URL, {}) == AccountMode.BUYER_DECLARED
    monkeypatch.setenv(f"NXFLO_ACCOUNT_MODE_{SLUG}", "operator_authorized")
    assert determine_account_mode(URL, {}) == AccountMode.OPERATOR_AUTHORIZED
    monkeypatch.delenv(f"NXFLO_ACCOUNT_MODE_{SLUG}")
    monkeypatch.setattr(settings, "account_mode", "buyer_declared")
    assert determine_account_mode(URL, {"account": {"require_operator_auth": True}}) == AccountMode.BUYER_DECLARED


async def test_buyer_declared_provisions_once_and_sends_natural_key() -> None:
    fake = FakeSeller({"sync_accounts": [synced("active")]})
    manager = AccountManager(fake)
    seller = buyer_declared()
    ref = await manager.resolve_account_ref(seller, BRAND)
    assert ref == {"brand": {"domain": "acme.example"}, "operator": "nxflo.io"}  # never account_id
    tool, params = fake.calls[0]
    assert tool == "sync_accounts"
    assert params["accounts"] == [{"brand": {"domain": "acme.example"}, "operator": "nxflo.io", "billing": "operator"}]
    assert len(params["idempotency_key"]) >= 16 and params["adcp_version"] == "3.2"
    assert await manager.resolve_account_ref(seller, BRAND) == ref
    assert len(fake.calls) == 1


async def test_pending_approval_fails_closed_then_activates() -> None:
    pending = synced("pending_approval", setup={"url": "https://seller.example/onboard", "message": "credit"})
    active = {"accounts": [{"brand": BRAND, "operator": "nxflo.io", "status": "active", "billing": "operator"}]}
    fake = FakeSeller({"sync_accounts": [pending], "list_accounts": [active]})
    manager = AccountManager(fake)
    seller = buyer_declared()
    with pytest.raises(AccountError) as info:
        await manager.resolve_account_ref(seller, BRAND)
    assert info.value.code == "account_not_usable" and "onboard" in str(info.value)
    ref = await manager.resolve_account_ref(seller, BRAND)
    assert ref["operator"] == "nxflo.io"
    assert [c[0] for c in fake.calls] == ["sync_accounts", "list_accounts"]
    assert fake.calls[1][1]["account"] == ref


async def test_billing_is_never_remapped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "account_billing", "advertiser")
    fake = FakeSeller({})
    with pytest.raises(AccountError) as info:
        await AccountManager(fake).resolve_account_ref(buyer_declared(), BRAND)
    assert info.value.code == "billing_not_supported" and fake.calls == []


async def test_sandbox_currency_and_timezone_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "account_sandbox", True)
    with pytest.raises(AccountError):
        AccountManager(FakeSeller({})).natural_key(buyer_declared(sandbox=False), BRAND)
    key = AccountManager(FakeSeller({})).natural_key(buyer_declared(sandbox=True), BRAND)
    assert key["sandbox"] is True
    monkeypatch.setattr(settings, "account_sandbox", False)

    fixed = buyer_declared(supported_account_currency_modes=["fixed"])
    with pytest.raises(AccountError):
        AccountManager(FakeSeller({})).natural_key(fixed, BRAND)
    monkeypatch.setattr(settings, "account_currency", "eur")
    assert AccountManager(FakeSeller({})).natural_key(fixed, BRAND)["currency"] == "EUR"
    both = buyer_declared(supported_account_currency_modes=["fixed", "per_media_buy"])
    assert "currency" not in AccountManager(FakeSeller({})).natural_key(both, BRAND)

    tz = buyer_declared(timezone={"mode": "account_fixed", "account_selection": "buyer_selected",
                                  "supported_timezones": ["UTC", "America/New_York"]})
    with pytest.raises(AccountError):
        AccountManager(FakeSeller({})).natural_key(tz, BRAND)
    monkeypatch.setattr(settings, "account_timezone", "UTC")
    assert AccountManager(FakeSeller({})).natural_key(tz, BRAND)["timezone"] == "UTC"


async def test_lazy_provisioning_seller_gets_natural_key_without_calls() -> None:
    seller = buyer_declared()
    seller.tools = ["get_products", "create_media_buy", "list_accounts"]
    fake = FakeSeller({})
    ref = await AccountManager(fake).resolve_account_ref(seller, BRAND)
    assert ref["brand"] == BRAND and fake.calls == []


async def test_discovery_omits_unprovisioned_accounts() -> None:
    fake = FakeSeller({"sync_accounts": [synced("active")]})
    manager = AccountManager(fake)
    seller = buyer_declared()
    assert await manager.discovery_account_ref(seller, BRAND) is None
    assert fake.calls == []
    await manager.resolve_account_ref(seller, BRAND)
    assert await manager.discovery_account_ref(seller, BRAND) == {"brand": BRAND, "operator": "nxflo.io"}


async def test_required_for_products_provisions_before_discovery() -> None:
    fake = FakeSeller({"sync_accounts": [synced("active")]})
    ref = await AccountManager(fake).discovery_account_ref(buyer_declared(required_for_products=True), BRAND)
    assert ref is not None and fake.calls[0][0] == "sync_accounts"


async def test_operator_mode_requires_operator_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(f"NXFLO_OPERATOR_TOKEN_{SLUG}", raising=False)
    seller = operator_authorized(monkeypatch, token=None)
    with pytest.raises(AccountError) as info:
        await AccountManager(FakeSeller({})).resolve_account_ref(seller, BRAND)
    assert info.value.code == "operator_credential_missing" and f"NXFLO_OPERATOR_TOKEN_{SLUG}" in str(info.value)


async def test_operator_mode_uses_operator_credential_and_account_id(monkeypatch: pytest.MonkeyPatch) -> None:
    seller = operator_authorized(monkeypatch)
    assert _build_headers(seller) == {"Authorization": "Bearer op-credential"}
    listing = {"accounts": [{"account_id": "acc_social_1", "name": "Acme social", "status": "active",
                             "brand": BRAND, "operator": "nxflo.io", "billing": "operator"}]}
    fake = FakeSeller({"list_accounts": [listing]})
    assert await AccountManager(fake).resolve_account_ref(seller, BRAND) == {"account_id": "acc_social_1"}
    with pytest.raises(AccountError):
        await AccountManager(fake).sync_accounts(seller, BRAND)


async def test_operator_mode_ambiguity_and_pinning(monkeypatch: pytest.MonkeyPatch) -> None:
    seller = operator_authorized(monkeypatch)
    listing = {"accounts": [
        {"account_id": "a1", "status": "active", "brand": {"domain": "other.example"}, "operator": "nxflo.io"},
        {"account_id": "a2", "status": "active", "brand": {"domain": "third.example"}, "operator": "nxflo.io"},
    ]}
    with pytest.raises(AccountError) as info:
        await AccountManager(FakeSeller({"list_accounts": [listing]})).resolve_account_ref(seller, BRAND)
    assert info.value.code == "account_ambiguous" and info.value.details["candidates"] == ["a1", "a2"]
    monkeypatch.setenv(f"NXFLO_OPERATOR_ACCOUNT_ID_{SLUG}", "acc_pinned")
    fake = FakeSeller({})
    assert await AccountManager(fake).resolve_account_ref(seller, BRAND) == {"account_id": "acc_pinned"}
    assert fake.calls == []


async def test_agent_credential_used_in_buyer_declared_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(f"NXFLO_OPERATOR_TOKEN_{SLUG}", "op-credential")
    seller = buyer_declared()
    seller.token = "agent-credential"
    assert _build_headers(seller) == {"Authorization": "Bearer agent-credential"}


async def test_list_accounts_follows_pagination() -> None:
    page1 = {"accounts": [{"account_id": "a1", "status": "active", "brand": BRAND, "operator": "nxflo.io"}],
             "pagination": {"has_more": True, "cursor": "c2"}}
    page2 = {"accounts": [{"account_id": "a2", "status": "pending_approval",
                           "brand": {"domain": "b.example"}, "operator": "nxflo.io"}],
             "pagination": {"has_more": False}}
    fake = FakeSeller({"list_accounts": [page1, page2]})
    manager = AccountManager(fake)
    accounts = await manager.list_accounts(buyer_declared())
    assert [a.account_id for a in accounts] == ["a1", "a2"]
    assert fake.calls[1][1]["pagination"] == {"cursor": "c2"}
    assert len(manager.accounts_for(URL)) == 2


async def test_list_accounts_error_response_raises() -> None:
    fake = FakeSeller({"list_accounts": [{"errors": [{"code": "AUTH_MISSING", "message": "no"}]}]})
    with pytest.raises(AccountError):
        await AccountManager(fake).list_accounts(buyer_declared())


async def test_accounts_persist_and_reload(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from src.models import schema

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'accounts.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(schema.Base.metadata.create_all)
    monkeypatch.setattr(schema, "async_session", async_sessionmaker(engine, expire_on_commit=False))

    manager = AccountManager(FakeSeller({"sync_accounts": [synced("active")]}))
    manager._db_enabled = True
    ref = await manager.resolve_account_ref(buyer_declared(), BRAND)

    reloaded = AccountManager(FakeSeller({}))
    await reloaded.init_db()
    assert await reloaded.resolve_account_ref(buyer_declared(), BRAND) == ref
    [account] = reloaded.accounts_for(URL)
    assert account.status == "active" and account.account_id == "acc_1"
    await engine.dispose()
