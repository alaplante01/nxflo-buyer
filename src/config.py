"""Configuration for the Nexflo buying agent."""

import os
import re

from pydantic import Field
from pydantic_settings import BaseSettings


class SellerConfig:
    """Configuration for a single seller agent."""

    def __init__(self, name: str, url: str, token: str | None = None, enabled: bool = True) -> None:
        self.name = name
        self.url = url
        self.token = token
        self.enabled = enabled


# Pre-configured sellers (add more as you get auth tokens)
# Tokens can also be set via env: NXFLO_SELLER_TOKEN_<slug>=<token>, where <slug> is the
# seller host upper-cased with non-alphanumerics replaced by "_" (see seller_env_slug).
DEFAULT_SELLERS = [
    SellerConfig(
        name="AdCP Test Agent",
        url="https://test-agent.adcontextprotocol.org/mcp",
        token=os.environ.get("NXFLO_SELLER_TOKEN_ADCP_TEST"),
    ),
    # Live sellers from AdCP registry (auth tokens TBD)
    SellerConfig(name="Adzymic SPH", url="https://sph.sales-agent.adzymic.ai/mcp"),
    SellerConfig(name="Adzymic MediaCorp", url="https://mediacorp.sales-agent.adzymic.ai/mcp"),
    SellerConfig(name="Adzymic APX", url="https://apx.sales-agent.adzymic.ai/mcp"),
    SellerConfig(name="Adzymic TSL", url="https://tsl.sales-agent.adzymic.ai/mcp"),
    SellerConfig(name="Bidcliq", url="https://agents.bidcliq.com/mcp"),
    SellerConfig(name="Content Ignite", url="https://sales-agent.contentignite.com/mcp"),
    SellerConfig(name="Advertible", url="https://adcp.4dvertible.com/mcp"),
    SellerConfig(name="Swivel", url="https://adcp-mcp-server-286099387629.us-central1.run.app/mcp"),
]


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = {"env_prefix": "NXFLO_"}

    # Server
    host: str = "0.0.0.0"
    port: int = 8000

    # Database (SQLite for MVP, PostgreSQL later)
    database_url: str = Field(default="sqlite+aiosqlite:///nxflo.db")

    # AdCP Registry (primary + fallback; both serve GET {base}/agents)
    registry_url: str = "https://agenticadvertising.org/api/registry"
    registry_fallback_url: str = "https://adcontextprotocol.org/api/registry"

    # AdCP release this buyer speaks (sent as adcp_version on 3.2-shaped requests)
    adcp_version: str = "3.2"

    # MCP Client
    mcp_timeout: int = 30
    mcp_max_retries: int = 3  # clamped to the spec ceiling of 3
    mcp_retry_budget_seconds: float = 300.0  # clamped to the spec ceiling of 300 s

    # Brand manifest (legacy default advertiser; brand domain derives from brand_url)
    brand_name: str = "Nexflo"
    brand_url: str = "https://nexflo.ai"

    # Our identity as an operator/agent (brand.json authorized_operators, account natural key)
    operator_domain: str = "nxflo.io"
    # Public URL of this buyer's MCP endpoint (must equal our brand.json agents[].url)
    agent_url: str = "https://buyer.nxflo.io/mcp"
    # Operator brand.json URL advertised as identity.brand_json_url; empty = derived
    brand_json_url: str = ""
    # supported_protocols for our own get_adcp_capabilities (schema needs >= 1 value)
    agent_supported_protocols: list[str] = Field(default_factory=lambda: ["media_buy"])

    # Accounts (AdCP 3.2 account model)
    account_mode: str = "auto"  # auto | buyer_declared | operator_authorized
    account_billing: str = "operator"  # operator | agent | advertiser (never remapped)
    account_sandbox: bool = False  # use sandbox accounts (natural key sandbox: true)
    account_auto_provision: bool = True  # sync_accounts before the first billable call
    account_currency: str = ""  # ISO 4217; only sent to sellers whose accounts are fixed-currency
    account_timezone: str = ""  # only sent when a seller requires a buyer-selected timezone

    # Brand Protocol (brand.json resolution cache)
    brand_cache_ttl_seconds: int = 86400
    brand_negative_cache_ttl_seconds: int = 3600

    # Sales-agent authorization (adagents.json). Enforced unless explicitly skipped.
    skip_sales_agent_authorization_check: bool = False
    adagents_cache_ttl_seconds: int = 86400
    adagents_negative_cache_ttl_seconds: int = 3600

    # RFC 9421 request signing (optional in AdCP 3.2)
    signing_enabled: bool = False
    signing_private_key_path: str = ""  # PEM (PKCS#8) or private JWK file
    signing_private_key: str = ""  # inline PEM or private JWK (e.g. from a secret manager)
    signing_key_id: str = ""  # kid; falls back to the JWK's kid
    signing_additional_jwks_path: str = ""  # extra public keys to publish (rotation)

    # Webhook receiver (set webhook_base_url to enable webhooks)
    webhook_base_url: str = ""  # e.g., "https://nexflo.ai" — must be publicly reachable
    webhook_secret: str = ""  # HMAC shared secret (auto-generated if empty)
    webhook_auth_scheme: str = "HMAC-SHA256"  # or "bearer"

    # Rate limiting
    rate_limit_per_minute: int = 100

    def resolved_brand_json_url(self) -> str:
        """identity.brand_json_url for our capabilities response."""
        if self.brand_json_url:
            return self.brand_json_url
        return f"https://{self.operator_domain}/.well-known/brand.json"


settings = Settings()


def seller_env_slug(url: str) -> str:
    """Env-var slug for a seller URL: host upper-cased, non-alphanumerics -> '_'."""
    host = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", url).split("/", 1)[0].split("@")[-1]
    host = host.split(":", 1)[0]
    return re.sub(r"[^A-Z0-9]", "_", host.upper())


def seller_env(prefix: str, url: str) -> str | None:
    """Read a per-seller env var, e.g. seller_env("NXFLO_OPERATOR_TOKEN", url)."""
    value = os.environ.get(f"{prefix}_{seller_env_slug(url)}")
    return value or None
