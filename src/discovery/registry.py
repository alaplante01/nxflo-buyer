"""Discover seller agents from the AdCP registry and direct configuration.

Discovery uses multiple mechanisms (per AdCP MCP Guide):
1. Local config (pre-configured sellers with auth tokens)
2. AdCP registry API
3. Server card (/.well-known/mcp.json)
4. get_adcp_capabilities tool (protocol-recommended discovery)
"""

import asyncio
import logging
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

from src.config import DEFAULT_SELLERS, settings

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

    # Capabilities from get_adcp_capabilities
    adcp_versions: list[int] = field(default_factory=list)
    supported_protocols: list[str] = field(default_factory=list)
    capabilities: dict = field(default_factory=dict)
    portfolio: dict = field(default_factory=dict)
    extensions_supported: list[str] = field(default_factory=list)
    server_card: dict | None = None

    @property
    def can_sell(self) -> bool:
        """Whether this agent supports the media buy workflow.

        Prefers capabilities-based check, falls back to tool-based.
        """
        if self.supported_protocols:
            return "media_buy" in self.supported_protocols
        return bool(SALES_TOOLS & set(self.tools))

    @property
    def supports_media_buy(self) -> bool:
        return "media_buy" in self.supported_protocols

    @property
    def supports_signals(self) -> bool:
        if self.supported_protocols:
            return "signals" in self.supported_protocols
        return bool(SIGNALS_TOOLS & set(self.tools))

    @property
    def supports_governance(self) -> bool:
        return "governance" in self.supported_protocols

    @property
    def has_signals(self) -> bool:
        return self.supports_signals

    @property
    def has_creatives(self) -> bool:
        return bool(CREATIVE_TOOLS & set(self.tools))


async def fetch_server_card(base_url: str) -> dict | None:
    """Check /.well-known/mcp.json and /.well-known/server.json on seller domain.

    Returns the server card dict if found, None otherwise.
    Extracts _meta.adcontextprotocol.org for AdCP-specific metadata.
    """
    parsed = urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        for path in ("/.well-known/mcp.json", "/.well-known/server.json"):
            try:
                resp = await client.get(f"{origin}{path}")
                if resp.status_code == 200:
                    data = resp.json()
                    logger.debug(f"Found server card at {origin}{path}")
                    return data
            except Exception:
                continue

    return None


def _extract_adcp_meta(server_card: dict) -> dict:
    """Extract AdCP metadata from a server card's _meta field."""
    meta = server_card.get("_meta", {})
    return meta.get("adcontextprotocol.org", {})


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
    """Probe a seller to discover its tools, capabilities, and status.

    Steps:
    1. Connect via MCP and list tools
    2. If get_adcp_capabilities is available, call it for rich discovery
    3. Optionally fetch server card from well-known URL
    4. Classify agent type based on capabilities or tools
    """
    from src.connections.seller import connect_to_seller, get_adcp_capabilities_tool

    try:
        async with connect_to_seller(agent) as client:
            tools = await client.list_tools()
            agent.tools = [t.name for t in tools] if tools else []
            agent.status = "online"

            # Classify by tools (baseline)
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

    # If get_adcp_capabilities is available, call it for rich discovery
    if "get_adcp_capabilities" in agent.tools:
        try:
            caps = await get_adcp_capabilities_tool(agent)
            agent.capabilities = caps

            # Extract structured fields
            adcp_info = caps.get("adcp", {})
            agent.adcp_versions = adcp_info.get("major_versions", [])
            agent.supported_protocols = caps.get("supported_protocols", [])
            agent.extensions_supported = caps.get("extensions_supported", [])

            # Extract media buy portfolio if present
            media_buy = caps.get("media_buy", {})
            if media_buy:
                agent.portfolio = media_buy.get("portfolio", {})

            # Re-classify based on capabilities
            if agent.supported_protocols:
                if "media_buy" in agent.supported_protocols:
                    agent.agent_type = "sales"
                elif "signals" in agent.supported_protocols:
                    agent.agent_type = "signals"

            logger.info(
                f"  {agent.name} capabilities: protocols={agent.supported_protocols}, "
                f"extensions={agent.extensions_supported}"
            )

        except Exception as e:
            logger.debug(f"get_adcp_capabilities failed for {agent.name}: {e}")

    # Try fetching server card (non-blocking, best-effort)
    try:
        card = await fetch_server_card(agent.url)
        if card:
            agent.server_card = card
            adcp_meta = _extract_adcp_meta(card)
            if adcp_meta and not agent.supported_protocols:
                agent.supported_protocols = adcp_meta.get("protocols_supported", [])
                agent.extensions_supported = adcp_meta.get("extensions_supported", [])
    except Exception:
        pass

    return agent


async def discover_all_sellers(probe: bool = False) -> list[SellerAgent]:
    """Discover sellers from all sources: config + registry.

    Config sellers take priority (they have auth tokens).
    Registry sellers are added if not already in config.

    If probe=True, connects to each seller to discover tools and status.
    """
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
