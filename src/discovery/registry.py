"""Discover seller agents from the AdCP registry and direct configuration."""

import logging
from dataclasses import dataclass, field

import httpx

from src.config import DEFAULT_SELLERS, SellerConfig, settings

logger = logging.getLogger(__name__)

# Tools that indicate a sales-capable agent
SALES_TOOLS = {"get_products", "create_media_buy", "get_media_buy_delivery"}
SIGNALS_TOOLS = {"get_signals"}
CREATIVE_TOOLS = {"list_creative_formats", "sync_creatives", "preview_creative"}


@dataclass
class SellerAgent:
    """A discovered seller agent with its capabilities."""

    name: str
    url: str
    token: str | None = None
    agent_type: str = "sales"  # sales, signals, creative
    tools: list[str] = field(default_factory=list)
    status: str = "unknown"  # online, offline, error
    source: str = "config"  # config, registry, discovery

    @property
    def can_sell(self) -> bool:
        """Whether this agent supports the media buy workflow."""
        return bool(SALES_TOOLS & set(self.tools))

    @property
    def has_signals(self) -> bool:
        return bool(SIGNALS_TOOLS & set(self.tools))

    @property
    def has_creatives(self) -> bool:
        return bool(CREATIVE_TOOLS & set(self.tools))


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


async def probe_seller(agent: SellerAgent) -> SellerAgent:
    """Probe a seller to discover its tools and status.

    Connects via MCP, lists tools, and classifies the agent type.
    """
    # Import here to avoid circular imports
    from src.connections.seller import connect_to_seller

    try:
        async with connect_to_seller(agent) as client:
            tools = await client.list_tools()
            agent.tools = [t.name for t in tools] if tools else []
            agent.status = "online"

            # Classify by tools
            tool_set = set(agent.tools)
            if SALES_TOOLS & tool_set:
                agent.agent_type = "sales"
            elif SIGNALS_TOOLS & tool_set:
                agent.agent_type = "signals"
            elif CREATIVE_TOOLS & tool_set:
                agent.agent_type = "creative"

    except Exception as e:
        agent.status = "error"
        logger.debug(f"Probe failed for {agent.name}: {e}")

    return agent


async def discover_all_sellers(probe: bool = False) -> list[SellerAgent]:
    """Discover sellers from all sources: config + registry.

    Config sellers take priority (they have auth tokens).
    Registry sellers are added if not already in config.

    If probe=True, connects to each seller to discover tools and status.
    """
    import asyncio

    sellers = get_configured_sellers()
    # Normalize URLs for dedup: strip trailing slash and /mcp suffix
    def _normalize_url(url: str) -> str:
        url = url.rstrip("/")
        if url.endswith("/mcp"):
            url = url[:-4]
        return url.lower()

    known_urls = {_normalize_url(s.url) for s in sellers}

    registry_agents = await fetch_registry_agents()
    for agent in registry_agents:
        norm = _normalize_url(agent.url)
        if norm not in known_urls:
            sellers.append(agent)
            known_urls.add(norm)

    if probe:
        sellers = await asyncio.gather(*[probe_seller(s) for s in sellers])
        sellers = list(sellers)
        online = sum(1 for s in sellers if s.status == "online")
        sales = sum(1 for s in sellers if s.can_sell)
        logger.info(f"Probed {len(sellers)} sellers: {online} online, {sales} can sell")
    else:
        logger.info(
            f"Total sellers: {len(sellers)} ({sum(1 for s in sellers if s.token)} with auth)"
        )

    return sellers
