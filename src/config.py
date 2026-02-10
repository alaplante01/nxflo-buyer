"""Configuration for the ADFX Buying Agent."""

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
DEFAULT_SELLERS = [
    SellerConfig(
        name="AdCP Test Agent",
        url="https://test-agent.adcontextprotocol.org/mcp",
        token="1v8tAhASaUYYp4odoQ1PnMpdqNaMiTrCRqYo9OJp6IQ",
    ),
]


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = {"env_prefix": "ADFX_"}

    # Server
    host: str = "0.0.0.0"
    port: int = 8000

    # Database (SQLite for MVP, PostgreSQL later)
    database_url: str = Field(default="sqlite+aiosqlite:///adfx_buyer.db")

    # AdCP Registry
    registry_url: str = "https://adcontextprotocol.org/api/registry"

    # MCP Client
    mcp_timeout: int = 30
    mcp_max_retries: int = 3

    # Brand manifest (default for ADFX)
    brand_name: str = "ADFX"
    brand_url: str = "https://adfx.io"


settings = Settings()
