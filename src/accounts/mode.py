"""Account model selection per seller (buyer-declared vs operator-authorized)."""

from enum import StrEnum
from typing import Any

from src.config import seller_env, settings


class AccountMode(StrEnum):
    """How account references are formed for a seller.

    - BUYER_DECLARED (``require_operator_auth: false``): our agent credential; accounts
      are provisioned with ``sync_accounts`` and referenced by natural key.
    - OPERATOR_AUTHORIZED (``require_operator_auth: true``): an operator credential;
      accounts are discovered with ``list_accounts`` and referenced by ``account_id``.
    """

    BUYER_DECLARED = "buyer_declared"
    OPERATOR_AUTHORIZED = "operator_authorized"


def determine_account_mode(seller_url: str, capabilities: dict[str, Any] | None) -> AccountMode:
    """Per-seller override (NXFLO_ACCOUNT_MODE_<SLUG>), global setting, then capabilities."""
    configured = seller_env("NXFLO_ACCOUNT_MODE", seller_url) or settings.account_mode
    if configured and configured != "auto":
        return AccountMode(configured)
    account = (capabilities or {}).get("account")
    if isinstance(account, dict) and account.get("require_operator_auth") is True:
        return AccountMode.OPERATOR_AUTHORIZED
    return AccountMode.BUYER_DECLARED
