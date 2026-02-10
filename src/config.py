"""Configuration for the Nexflo buying agent."""

from pydantic import Field
from pydantic_settings import BaseSettings


class SellerConfig:
    """Configuration for a single seller agent."""

    def __init__(self, name: str, url: str, token: str | None = None, enabled: bool = True):
        self.name = name
        self.url = url
        self.token = token
        self.enabled = enabled


# Pre-configured sellers (add more as you get auth tokens)
# Tokens can also be set via env: NXFLO_SELLER_TOKEN_<slug>=<token>
DEFAULT_SELLERS = [
    SellerConfig(
        name="AdCP Test Agent",
        url="https://test-agent.adcontextprotocol.org/mcp",
        token="1v8tAhASaUYYp4odoQ1PnMpdqNaMiTrCRqYo9OJp6IQ",
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

    # AdCP Registry
    registry_url: str = "https://adcontextprotocol.org/api/registry"

    # MCP Client
    mcp_timeout: int = 30
    mcp_max_retries: int = 3

    # Brand manifest
    brand_name: str = "Nexflo"
    brand_url: str = "https://nexflo.io"


settings = Settings()
