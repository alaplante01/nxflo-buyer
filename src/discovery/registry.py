"""Discover seller agents from the AdCP registry and direct configuration."""

import logging
from dataclasses import dataclass, field

import httpx

from src.config import DEFAULT_SELLERS, SellerConfig, settings

logger = logging.getLogger(__name__)


@dataclass
class SellerAgent:
    """A discovered seller agent with its capabilities."""

    name: str
    url: str
    token: str | None = None
    agent_type: str = "sales"
    tools: list[str] = field(default_factory=list)
    status: str = "unknown"  # online, offline, unknown
    source: str = "config"  # config, registry, discovery


async def fetch_registry_agents() -> list[SellerAgent]:
    """Fetch seller agents from the AdCP public registry.

    Calls GET /api/registry/agents and filters for sales-type agents.
    """
    agents: list[SellerAgent] = []

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{settings.registry_url}/agents")
            resp.raise_for_status()
            data = resp.json()

            for agent in data if isinstance(data, list) else data.get("agents", []):
                agent_type = agent.get("type", "unknown")
                if agent_type not in ("sales", "unknown"):
                    continue

                url = agent.get("url") or agent.get("mcp_endpoint") or ""
                if not url:
                    continue

                agents.append(
                    SellerAgent(
                        name=agent.get("name", "Unknown"),
                        url=url,
                        agent_type=agent_type,
                        tools=agent.get("tools", []),
                        status=agent.get("health", {}).get("status", "unknown")
                        if isinstance(agent.get("health"), dict)
                        else "unknown",
                        source="registry",
                    )
                )

            logger.info(f"Discovered {len(agents)} seller agents from registry")

    except Exception as e:
        logger.warning(f"Failed to fetch registry agents: {e}")

    return agents


def get_configured_sellers() -> list[SellerAgent]:
    """Get locally configured seller agents."""
    return [
        SellerAgent(
            name=s.name,
            url=s.url,
            token=s.token,
            source="config",
            status="unknown",
        )
        for s in DEFAULT_SELLERS
        if s.enabled
    ]


async def discover_all_sellers() -> list[SellerAgent]:
    """Discover sellers from all sources: config + registry.

    Config sellers take priority (they have auth tokens).
    Registry sellers are added if not already in config.
    """
    sellers = get_configured_sellers()
    known_urls = {s.url.rstrip("/") for s in sellers}

    registry_agents = await fetch_registry_agents()
    for agent in registry_agents:
        if agent.url.rstrip("/") not in known_urls:
            sellers.append(agent)
            known_urls.add(agent.url.rstrip("/"))

    logger.info(f"Total sellers: {len(sellers)} ({sum(1 for s in sellers if s.token)} with auth)")
    return sellers
