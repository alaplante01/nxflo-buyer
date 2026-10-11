"""AdCP 3.2 account management per seller: list_accounts, sync_accounts, account refs.

Two seller models (docs/accounts/overview, docs/building/by-layer/L2/accounts-and-agents):

- Buyer-declared (``require_operator_auth: false``): we authenticate as our agent,
  provision with ``sync_accounts`` (brand + operator + billing [+ sandbox ...]) and
  send the *natural key* as ``account`` on every account-scoped call. A seller-issued
  ``account_id`` is a correlation handle only and is never sent in this mode.
- Operator-authorized (``require_operator_auth: true``): calls use the operator's
  credential (NXFLO_OPERATOR_TOKEN_<SLUG>), the seller owns the namespace, and we send
  ``{"account_id": ...}`` resolved via ``list_accounts`` (or pinned with
  NXFLO_OPERATOR_ACCOUNT_ID_<SLUG>).

Discovery calls omit ``account`` until the account is provisioned and its status
allows the task; an unresolved account is an error, never a silent fallback.
"""

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from src.accounts.mode import AccountMode
from src.buying.requests import new_idempotency_key, with_version
from src.config import seller_env, seller_env_slug, settings
from src.discovery.registry import SellerAgent
from src.utils import utcnow

logger = logging.getLogger(__name__)

ToolCaller = Callable[[SellerAgent, str, dict[str, Any]], Awaitable[dict[str, Any]]]

# Account status gates (accounts overview, "Operations by Account Status").
TASK_ALLOWED_STATUSES: dict[str, frozenset[str]] = {
    "get_products": frozenset({"active", "payment_required"}),
    "create_media_buy": frozenset({"active"}),
    "update_media_buy": frozenset({"active", "payment_required"}),
    "sync_creatives": frozenset({"active", "payment_required"}),
}
BILLING_PARTIES = frozenset({"operator", "agent", "advertiser"})


class AccountError(Exception):
    """An account reference could not be established for a seller."""

    def __init__(
        self, code: str, seller_name: str, message: str, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(f"[{code}] {seller_name}: {message}")
        self.code = code
        self.details = details or {}


def canonical_key(ref: dict[str, Any]) -> str:
    return json.dumps(ref, sort_keys=True, separators=(",", ":"))


def brand_key(brand: dict[str, Any]) -> dict[str, Any]:
    """BrandKey projection: domain, optional brand_id, sorted unique countries."""
    key: dict[str, Any] = {"domain": str(brand["domain"]).lower()}
    if brand.get("brand_id"):
        key["brand_id"] = brand["brand_id"]
    if brand.get("countries"):
        key["countries"] = sorted({str(c).upper() for c in brand["countries"]})
    return key


@dataclass
class SellerAccount:
    """An account as returned by list_accounts / sync_accounts."""

    seller_url: str
    account_mode: str
    account_id: str | None = None
    name: str | None = None
    brand: dict[str, Any] | None = None
    operator: str | None = None
    operator_unit: dict[str, Any] | None = None
    currency: str | None = None
    timezone: str | None = None
    sandbox: bool = False
    status: str | None = None
    billing: str | None = None
    account_scope: str | None = None
    setup: dict[str, Any] | None = None
    authorization: dict[str, Any] | None = None
    errors: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    updated_at: datetime = field(default_factory=utcnow)
    natural: dict[str, Any] | None = None  # natural key per the seller's identity rules

    @classmethod
    def from_wire(cls, seller_url: str, mode: AccountMode, data: dict[str, Any]) -> "SellerAccount":
        echoed = data.get("account") if isinstance(data.get("account"), dict) else {}
        return cls(
            seller_url=seller_url,
            account_mode=mode.value,
            account_id=data.get("account_id") or echoed.get("account_id"),
            name=data.get("name"),
            brand=data.get("brand") or echoed.get("brand"),
            operator=data.get("operator") or echoed.get("operator"),
            operator_unit=data.get("operator_unit"),
            currency=data.get("currency"),
            timezone=data.get("timezone"),
            sandbox=bool(data.get("sandbox", echoed.get("sandbox", False))),
            status=data.get("status"),
            billing=data.get("billing"),
            account_scope=data.get("account_scope"),
            setup=data.get("setup") if isinstance(data.get("setup"), dict) else None,
            authorization=data.get("authorization")
            if isinstance(data.get("authorization"), dict)
            else None,
            errors=[e for e in data.get("errors") or [] if isinstance(e, dict)],
            raw=data,
        )

    def natural_key(self, *, timezone_in_key: bool = False) -> dict[str, Any] | None:
        """Complete buyer-declared natural key, when the account carries one.

        ``timezone`` participates only for sellers whose account timezone is
        buyer-selected; the manager records the seller-specific key in ``natural``.
        """
        if self.natural is not None:
            return self.natural
        if not self.brand or not self.operator:
            return None
        key: dict[str, Any] = {"brand": brand_key(self.brand), "operator": self.operator}
        if self.operator_unit and self.operator_unit.get("id"):
            key["operator_unit"] = {"id": self.operator_unit["id"]}
        if self.currency:
            key["currency"] = self.currency
        if self.timezone and timezone_in_key:
            key["timezone"] = self.timezone
        if self.sandbox:
            key["sandbox"] = True
        return key

    @property
    def storage_key(self) -> str:
        if self.account_mode == AccountMode.BUYER_DECLARED.value:
            natural = self.natural_key()
            if natural is not None:
                return canonical_key(natural)
        return f"id:{self.account_id}"

    def account_ref(self) -> dict[str, Any]:
        """The AccountRef to send on protocol tasks for this account's mode."""
        if self.account_mode == AccountMode.OPERATOR_AUTHORIZED.value and self.account_id:
            return {"account_id": self.account_id}
        natural = self.natural_key()
        if natural is None:
            raise ValueError("buyer-declared account has no natural key")
        return natural

    def to_api(self) -> dict[str, Any]:
        return {
            "seller_url": self.seller_url,
            "account_mode": self.account_mode,
            "account_id": self.account_id,
            "name": self.name,
            "brand": self.brand,
            "operator": self.operator,
            "sandbox": self.sandbox,
            "status": self.status,
            "billing": self.billing,
            "account_scope": self.account_scope,
            "setup": self.setup,
            "authorization": self.authorization,
            "errors": self.errors,
            "updated_at": self.updated_at.isoformat(),
        }


class AccountManager:
    """Tracks accounts per seller and produces the AccountRef for each call."""

    def __init__(self, call_tool: ToolCaller, *, operator_domain: str | None = None) -> None:
        self._call_tool = call_tool
        self.operator_domain = (operator_domain or settings.operator_domain).lower()
        self._accounts: dict[str, dict[str, SellerAccount]] = {}
        self._db_enabled = False

    # --- persistence ---

    async def init_db(self) -> None:
        """Load persisted accounts (tables are created by the tracker's migrations)."""
        from sqlalchemy import select

        from src.models.schema import SellerAccountRecord, async_session

        self._db_enabled = True
        async with async_session() as session:
            result = await session.execute(select(SellerAccountRecord))
            for row in result.scalars():
                mode = AccountMode(row.account_mode or AccountMode.BUYER_DECLARED.value)
                account = SellerAccount.from_wire(row.seller_url, mode, row.raw or {})
                account.brand = account.brand or row.brand
                account.operator = account.operator or row.operator
                if not str(row.account_key).startswith("id:"):
                    account.natural = json.loads(row.account_key)
                account.updated_at = row.updated_at or account.updated_at
                self._remember(account)
        logger.info("Loaded %d seller accounts", sum(len(v) for v in self._accounts.values()))

    async def _persist(self, account: SellerAccount) -> None:
        if not self._db_enabled:
            return
        from src.models.schema import SellerAccountRecord, async_session

        async with async_session() as session:
            record = await session.get(
                SellerAccountRecord, (account.seller_url, account.storage_key)
            )
            if record is None:
                record = SellerAccountRecord(
                    seller_url=account.seller_url, account_key=account.storage_key
                )
                session.add(record)
            record.account_id = account.account_id
            record.name = account.name
            record.brand = account.brand
            record.operator = account.operator
            record.sandbox = int(account.sandbox)
            record.status = account.status
            record.billing = account.billing
            record.account_mode = account.account_mode
            record.raw = account.raw
            record.updated_at = account.updated_at
            await session.commit()

    def _remember(self, account: SellerAccount) -> None:
        self._accounts.setdefault(account.seller_url.rstrip("/"), {})[account.storage_key] = account

    async def _store(self, account: SellerAccount) -> SellerAccount:
        self._remember(account)
        await self._persist(account)
        return account

    def accounts_for(self, seller_url: str) -> list[SellerAccount]:
        return list(self._accounts.get(seller_url.rstrip("/"), {}).values())

    def all_accounts(self) -> list[SellerAccount]:
        return [a for per_seller in self._accounts.values() for a in per_seller.values()]

    # --- mode / natural key ---

    def mode_for(self, seller: SellerAgent) -> AccountMode:
        return seller.account_mode

    def natural_key(self, seller: SellerAgent, brand: dict[str, Any]) -> dict[str, Any]:
        """Buyer-declared natural key honoring the seller's currency/timezone/sandbox modes."""
        caps = seller.account_capabilities
        key: dict[str, Any] = {"brand": brand_key(brand), "operator": self.operator_domain}
        currency = self._currency_for(seller, caps)
        if currency:
            key["currency"] = currency
        timezone = self._timezone_for(seller, caps)
        if timezone:
            key["timezone"] = timezone
        if settings.account_sandbox:
            if caps and caps.get("sandbox") is not True:
                raise AccountError(
                    "sandbox_unsupported",
                    seller.name,
                    "seller does not declare account.sandbox: true",
                )
            key["sandbox"] = True
        return key

    def _currency_for(self, seller: SellerAgent, caps: dict[str, Any]) -> str | None:
        modes = caps.get("supported_account_currency_modes")
        if not isinstance(modes, list) or "per_media_buy" in modes or not modes:
            return None
        if not settings.account_currency:
            raise AccountError(
                "currency_required",
                seller.name,
                "seller accounts are fixed-currency; set NXFLO_ACCOUNT_CURRENCY",
            )
        return settings.account_currency.upper()

    def _timezone_for(self, seller: SellerAgent, caps: dict[str, Any]) -> str | None:
        tz_caps = caps.get("timezone")
        if not isinstance(tz_caps, dict) or tz_caps.get("account_selection") != "buyer_selected":
            return None
        allowed = tz_caps.get("supported_timezones") or []
        if not settings.account_timezone or (allowed and settings.account_timezone not in allowed):
            raise AccountError(
                "timezone_required",
                seller.name,
                f"seller needs a buyer-selected account timezone from {allowed}; set NXFLO_ACCOUNT_TIMEZONE",
            )
        return settings.account_timezone

    def _billing_for(self, seller: SellerAgent, requested: str | None) -> str:
        billing = (requested or settings.account_billing).lower()
        if billing not in BILLING_PARTIES:
            raise AccountError(
                "billing_invalid", seller.name, f"billing must be one of {sorted(BILLING_PARTIES)}"
            )
        supported = seller.account_capabilities.get("supported_billing")
        if isinstance(supported, list) and supported and billing not in supported:
            raise AccountError(
                "billing_not_supported",
                seller.name,
                f"seller supports billing {supported}; {billing!r} is never silently remapped",
            )
        return billing

    @staticmethod
    def _timezone_in_key(seller: SellerAgent) -> bool:
        tz_caps = seller.account_capabilities.get("timezone")
        return isinstance(tz_caps, dict) and tz_caps.get("account_selection") == "buyer_selected"

    def _from_wire(self, seller: SellerAgent, data: dict[str, Any]) -> SellerAccount:
        account = SellerAccount.from_wire(seller.url, self.mode_for(seller), data)
        if account.account_mode == AccountMode.BUYER_DECLARED.value:
            account.natural = account.natural_key(timezone_in_key=self._timezone_in_key(seller))
        return account

    def _find(self, seller_url: str, key: dict[str, Any]) -> SellerAccount | None:
        return self._accounts.get(seller_url.rstrip("/"), {}).get(canonical_key(key))

    # --- protocol tasks ---

    async def list_accounts(
        self,
        seller: SellerAgent,
        *,
        account: dict[str, Any] | None = None,
        status: str | None = None,
        sandbox: bool | None = None,
        max_pages: int = 10,
    ) -> list[SellerAccount]:
        """Call list_accounts (following pagination) and persist every returned account."""
        if not seller.has_tool("list_accounts"):
            raise AccountError(
                "list_accounts_unsupported", seller.name, "seller does not expose list_accounts"
            )
        found: list[SellerAccount] = []
        cursor: str | None = None
        for _ in range(max_pages):
            params: dict[str, Any] = {}
            if account:
                params["account"] = account
            if status:
                params["status"] = status
            if sandbox is not None:
                params["sandbox"] = sandbox
            if cursor:
                params["pagination"] = {"cursor": cursor}
            response = await self._call_tool(seller, "list_accounts", with_version(params))
            if response.get("errors") and "accounts" not in response:
                raise AccountError("list_accounts_failed", seller.name, str(response["errors"]))
            for item in response.get("accounts") or []:
                if isinstance(item, dict):
                    found.append(await self._store(self._from_wire(seller, item)))
            pagination = response.get("pagination") or {}
            cursor = pagination.get("cursor") if pagination.get("has_more") else None
            if not cursor:
                break
        return found

    async def sync_accounts(
        self,
        seller: SellerAgent,
        brand: dict[str, Any],
        *,
        billing: str | None = None,
        dry_run: bool = False,
    ) -> list[SellerAccount]:
        """Provision (buyer-declared) the brand/operator account on a seller."""
        if self.mode_for(seller) != AccountMode.BUYER_DECLARED:
            raise AccountError(
                "unsupported_provisioning",
                seller.name,
                "account-id namespaces are not provisioned via sync_accounts; use list_accounts",
            )
        if not seller.has_tool("sync_accounts"):
            raise AccountError(
                "sync_accounts_unsupported", seller.name, "seller does not expose sync_accounts"
            )
        key = self.natural_key(seller, brand)
        entry: dict[str, Any] = {**key, "billing": self._billing_for(seller, billing)}
        params: dict[str, Any] = {"idempotency_key": new_idempotency_key(), "accounts": [entry]}
        if dry_run:
            params["dry_run"] = True
        response = await self._call_tool(seller, "sync_accounts", with_version(params))
        if response.get("errors") and "accounts" not in response:
            raise AccountError("sync_accounts_failed", seller.name, str(response["errors"]))
        results: list[SellerAccount] = []
        for item in response.get("accounts") or []:
            if not isinstance(item, dict):
                continue
            account = self._from_wire(seller, item)
            # One entry was declared: track it under the natural key we sent, which the
            # seller MUST keep accepting, even if it echoes a canonicalized variant.
            account.brand = account.brand or key["brand"]
            account.operator = account.operator or key["operator"]
            account.sandbox = account.sandbox or bool(key.get("sandbox"))
            account.natural = key
            results.append(account if dry_run else await self._store(account))
        return results

    # --- account reference resolution ---

    async def resolve_account_ref(
        self,
        seller: SellerAgent,
        brand: dict[str, Any],
        *,
        task: str = "create_media_buy",
        provision: bool | None = None,
    ) -> dict[str, Any]:
        """AccountRef for an account-scoped call; raises AccountError (fail closed)."""
        if self.mode_for(seller) == AccountMode.OPERATOR_AUTHORIZED:
            return await self._resolve_account_id(seller, brand)
        allowed = TASK_ALLOWED_STATUSES.get(task, frozenset({"active"}))
        key = self.natural_key(seller, brand)
        known = self._find(seller.url, key)
        if known is not None and known.status in allowed:
            return key
        if known is not None and seller.has_tool("list_accounts"):
            refreshed = await self.list_accounts(seller, account=key)
            known = next((a for a in refreshed if a.storage_key == canonical_key(key)), known)
            if known.status in allowed:
                return key
        if known is None:
            do_provision = settings.account_auto_provision if provision is None else provision
            if not seller.has_tool("sync_accounts"):
                # Lazy-provisioning seller: the natural key provisions on the first
                # provisioning task (discovery never provisions).
                return key
            if not do_provision:
                raise AccountError(
                    "account_not_provisioned",
                    seller.name,
                    "account not provisioned; run sync_accounts",
                )
            synced = await self.sync_accounts(seller, brand)
            known = synced[0] if synced else None
            if known is not None and known.status in allowed:
                return key
        raise self._status_error(seller, known, task)

    def _status_error(
        self, seller: SellerAgent, account: SellerAccount | None, task: str
    ) -> AccountError:
        if account is None:
            return AccountError(
                "account_not_found", seller.name, "seller returned no account for the natural key"
            )
        if account.errors:
            return AccountError(
                "account_failed", seller.name, str(account.errors), {"errors": account.errors}
            )
        details: dict[str, Any] = {"status": account.status}
        if account.setup:
            details["setup"] = account.setup
        hint = (
            f" — complete setup at {account.setup.get('url')}"
            if account.setup and account.setup.get("url")
            else ""
        )
        return AccountError(
            "account_not_usable",
            seller.name,
            f"account status {account.status!r} does not allow {task}{hint}",
            details,
        )

    async def _resolve_account_id(
        self, seller: SellerAgent, brand: dict[str, Any]
    ) -> dict[str, Any]:
        slug = seller_env_slug(seller.url)
        if not seller.operator_token:
            raise AccountError(
                "operator_credential_missing",
                seller.name,
                f"seller requires operator auth; set NXFLO_OPERATOR_TOKEN_{slug}",
            )
        pinned = seller_env("NXFLO_OPERATOR_ACCOUNT_ID", seller.url)
        if pinned:
            return {"account_id": pinned}
        sandbox = True if settings.account_sandbox else None
        accounts = [a for a in await self.list_accounts(seller, sandbox=sandbox) if a.account_id]
        usable = [
            a
            for a in accounts
            if a.status == "active" and (not settings.account_sandbox or a.sandbox)
        ]
        if len(usable) == 1 and len(accounts) == 1:
            return {"account_id": usable[0].account_id}
        domain = str(brand.get("domain", "")).lower()
        matches = [
            a
            for a in usable
            if a.brand
            and str(a.brand.get("domain", "")).lower() == domain
            and (not brand.get("brand_id") or a.brand.get("brand_id") == brand.get("brand_id"))
        ]
        if len(matches) == 1:
            return {"account_id": matches[0].account_id}
        code = "account_ambiguous" if matches or len(usable) > 1 else "account_not_found"
        raise AccountError(
            code,
            seller.name,
            f"{len(matches)} active accounts match brand {domain}; pin one with NXFLO_OPERATOR_ACCOUNT_ID_{slug}",
            {"candidates": [a.account_id for a in usable]},
        )

    async def discovery_account_ref(
        self, seller: SellerAgent, brand: dict[str, Any]
    ) -> dict[str, Any] | None:
        """``account`` for get_products: only a provisioned, usable account, else None."""
        if seller.account_capabilities.get("required_for_products") is True:
            return await self.resolve_account_ref(seller, brand, task="get_products")
        if self.mode_for(seller) == AccountMode.OPERATOR_AUTHORIZED:
            pinned = seller_env("NXFLO_OPERATOR_ACCOUNT_ID", seller.url)
            return {"account_id": pinned} if pinned and seller.operator_token else None
        try:
            key = self.natural_key(seller, brand)
        except AccountError:
            logger.debug(
                "No natural key for %s discovery; omitting account", seller.name, exc_info=True
            )
            return None
        known = self._find(seller.url, key)
        if known is not None and known.status in TASK_ALLOWED_STATUSES["get_products"]:
            return key
        return None
