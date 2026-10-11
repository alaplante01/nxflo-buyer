"""Registry discovery: settings-driven base URL with fallback, entry parsing, sales filter."""

from typing import Any

import httpx
import pytest

from src.config import seller_env_slug, settings
from src.discovery import registry

ENTRIES = [
    {"name": "AdCP Test Agent", "url": "https://test-agent.adcontextprotocol.org/mcp", "type": "sales",
     "protocol": "mcp", "mcp_endpoint": "https://test-agent.adcontextprotocol.org/mcp",
     "member": {"slug": "bigads", "display_name": "BigAds"}, "contact": {"name": "BigAds"}, "description": "BigAds"},
    {"name": "A2A Seller", "url": "https://a2a.example/a2a", "type": "sales", "protocol": "a2a"},
    {"name": "Signals", "url": "https://signals.example/mcp", "type": "signals", "protocol": "mcp"},
    {"name": "Buyer", "url": "https://buyer.example/mcp", "type": "buying", "protocol": "mcp"},
    {"name": "Mystery", "url": "https://unknown.example/mcp", "type": "unknown", "protocol": "mcp"},
]


def test_default_registry_urls() -> None:
    assert registry.registry_base_urls() == [
        "https://agenticadvertising.org/api/registry",
        "https://adcontextprotocol.org/api/registry",
    ]


def test_parse_entry_shape_and_sales_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NXFLO_SELLER_TOKEN_TEST_AGENT_ADCONTEXTPROTOCOL_ORG", "from-env")
    parsed = [registry.parse_registry_entry(e) for e in ENTRIES]
    agent = parsed[0]
    assert agent is not None
    assert agent.url == "https://test-agent.adcontextprotocol.org/mcp"
    assert agent.member_slug == "bigads" and agent.member_name == "BigAds" and agent.source == "registry"
    assert agent.token == "from-env"
    assert parsed[1:] == [None, None, None, None]


def test_seller_env_slug() -> None:
    assert seller_env_slug("https://test-agent.adcontextprotocol.org/mcp") == "TEST_AGENT_ADCONTEXTPROTOCOL_ORG"
    assert seller_env_slug("https://user@Seller.Example:8443/x") == "SELLER_EXAMPLE"


async def test_falls_back_when_primary_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    async def listing(base_url: str) -> list[dict[str, Any]]:
        seen.append(base_url)
        if "agenticadvertising" in base_url:
            raise httpx.ConnectError("primary down")
        return ENTRIES

    monkeypatch.setattr(registry, "_fetch_registry_listing", listing)
    agents = await registry.fetch_registry_agents()
    assert [a.name for a in agents] == ["AdCP Test Agent"]
    assert seen == ["https://agenticadvertising.org/api/registry", "https://adcontextprotocol.org/api/registry"]


async def test_primary_success_skips_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    async def listing(base_url: str) -> list[dict[str, Any]]:
        seen.append(base_url)
        return ENTRIES

    monkeypatch.setattr(registry, "_fetch_registry_listing", listing)
    monkeypatch.setattr(settings, "registry_url", "https://registry.internal/api/registry/")
    await registry.fetch_registry_agents()
    assert seen == ["https://registry.internal/api/registry"]


async def test_no_registry_reachable_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    async def listing(base_url: str) -> list[dict[str, Any]]:
        raise httpx.ConnectError("down")

    monkeypatch.setattr(registry, "_fetch_registry_listing", listing)
    assert await registry.fetch_registry_agents() == []
