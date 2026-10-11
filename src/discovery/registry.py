"""Discover seller agents from the AdCP registry and direct configuration.

Discovery uses multiple mechanisms (per AdCP MCP Guide):
1. Local config (pre-configured sellers with auth tokens)
2. AdCP registry API: GET {registry}/agents on the primary registry
   (NXFLO_REGISTRY_URL), falling back to NXFLO_REGISTRY_FALLBACK_URL. Entries are
   shaped {name, url, type, protocol, mcp_endpoint, member{slug, display_name},
   contact, description}; only type == "sales" entries are bought from.
3. Server card (/.well-known/mcp.json)
4. get_adcp_capabilities tool (protocol-recommended discovery)
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx

from src.accounts.mode import AccountMode, determine_account_mode
from src.config import DEFAULT_SELLERS, seller_env, settings

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

    # Registry metadata
    protocol: str = "mcp"
    member_slug: str | None = None
    member_name: str | None = None
    description: str | None = None

    # Accounts: mode and the operator credential used in operator-authorized mode
    account_mode: AccountMode = AccountMode.BUYER_DECLARED
    operator_token: str | None = None

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

    @property
    def account_capabilities(self) -> dict[str, Any]:
        account = self.capabilities.get("account")
        return account if isinstance(account, dict) else {}

    @property
    def request_signing_capabilities(self) -> dict[str, Any]:
        signing = self.capabilities.get("request_signing")
        return signing if isinstance(signing, dict) else {}

    @property
    def idempotency_supported(self) -> bool:
        """True only when the seller explicitly declares idempotency replay support."""
        adcp = self.capabilities.get("adcp")
        idem = adcp.get("idempotency") if isinstance(adcp, dict) else None
        return isinstance(idem, dict) and idem.get("supported") is True

    @property
    def supported_versions(self) -> list[str]:
        adcp = self.capabilities.get("adcp")
        versions = adcp.get("supported_versions") if isinstance(adcp, dict) else None
        return [str(v) for v in versions] if isinstance(versions, list) else []

    def has_tool(self, name: str) -> bool:
        """Whether the seller exposes a tool; unknown tool lists are treated as present."""
        return not self.tools or name in self.tools

    def apply_account_config(self) -> None:
        """Derive account mode and operator credential from config + capabilities."""
        self.account_mode = determine_account_mode(self.url, self.capabilities)
        self.operator_token = seller_env("NXFLO_OPERATOR_TOKEN", self.url)


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
            except (httpx.HTTPError, ValueError):
                logger.debug("No server card at %s%s", origin, path, exc_info=True)
                continue

    return None


def _extract_adcp_meta(server_card: dict) -> dict:
    """Extract AdCP metadata from a server card's _meta field."""
    meta = server_card.get("_meta", {})
    return meta.get("adcontextprotocol.org", {})


def registry_base_urls() -> list[str]:
    """Primary registry first, then the fallback (deduplicated, empty values dropped)."""
    urls: list[str] = []
    for url in (settings.registry_url, settings.registry_fallback_url):
        base = (url or "").rstrip("/")
        if base and base not in urls:
            urls.append(base)
    return urls


def parse_registry_entry(entry: dict[str, Any]) -> SellerAgent | None:
    """Turn one registry /agents entry into a SellerAgent, or None if not buyable."""
    if entry.get("type") != "sales":
        return None
    protocol = str(entry.get("protocol") or "mcp").lower()
    url = entry.get("mcp_endpoint") or (entry.get("url") if protocol == "mcp" else None)
    if not url:
        logger.debug("Skipping non-MCP registry agent %s", entry.get("name"))
        return None
    member = entry.get("member") if isinstance(entry.get("member"), dict) else {}
    health = entry.get("health")
    return SellerAgent(
        name=entry.get("name", "Unknown"),
        url=url,
        token=seller_env("NXFLO_SELLER_TOKEN", url),
        agent_type="sales",
        tools=entry.get("tools", []) or [],
        status=health.get("status", "unknown") if isinstance(health, dict) else "unknown",
        source="registry",
        protocol=protocol,
        member_slug=member.get("slug"),
        member_name=member.get("display_name"),
        description=entry.get("description"),
    )


async def _fetch_registry_listing(base_url: str) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{base_url}/agents")
        resp.raise_for_status()
        data = resp.json()
    entries = data if isinstance(data, list) else data.get("agents", [])
    if not isinstance(entries, list):
        raise TypeError(f"unexpected registry payload from {base_url}")
    return [e for e in entries if isinstance(e, dict)]


async def fetch_registry_agents() -> list[SellerAgent]:
    """Fetch sales agents from the AdCP registry (primary, then fallback)."""
    for base_url in registry_base_urls():
        try:
            entries = await _fetch_registry_listing(base_url)
        except (httpx.HTTPError, ValueError, TypeError) as e:
            logger.warning("Registry %s unavailable: %s", base_url, e)
            continue
        agents = [a for a in (parse_registry_entry(e) for e in entries) if a is not None]
        logger.info(f"Discovered {len(agents)} sales agents from registry {base_url}")
        return agents
    logger.warning("No AdCP registry reachable; using configured sellers only")
    return []


def get_configured_sellers() -> list[SellerAgent]:
    """Get locally configured seller agents."""
    return [
        SellerAgent(
            name=s.name,
            url=s.url,
            token=s.token or seller_env("NXFLO_SELLER_TOKEN", s.url),
            source="config",
            status="unknown",
        )
        for s in DEFAULT_SELLERS
        if s.enabled
    ]


def _apply_capabilities(agent: SellerAgent, caps: dict[str, Any]) -> None:
    agent.capabilities = caps
    adcp_info = caps.get("adcp", {})
    agent.adcp_versions = adcp_info.get("major_versions", []) if isinstance(adcp_info, dict) else []
    agent.supported_protocols = caps.get("supported_protocols", [])
    agent.extensions_supported = caps.get("extensions_supported", [])
    media_buy = caps.get("media_buy", {})
    if isinstance(media_buy, dict) and media_buy:
        agent.portfolio = media_buy.get("portfolio", {})
    if agent.supported_protocols:
        if "media_buy" in agent.supported_protocols:
            agent.agent_type = "sales"
        elif "signals" in agent.supported_protocols:
            agent.agent_type = "signals"
    logger.info(
        f"  {agent.name} capabilities: protocols={agent.supported_protocols}, "
        f"extensions={agent.extensions_supported}"
    )


async def probe_seller(agent: SellerAgent) -> SellerAgent:
    """Probe a seller to discover its tools, capabilities, and status.

    Steps:
    1. Connect via MCP and list tools
    2. If get_adcp_capabilities is available, call it for rich discovery
    3. Optionally fetch server card from well-known URL
    4. Classify agent type based on capabilities or tools
    5. Derive the account mode / operator credential for this seller
    """
    from src.connections.seller import (
        SellerError,
        connect_to_seller,
        get_adcp_capabilities_tool,
    )

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

    except SellerError as e:
        agent.status = "error"
        logger.debug(f"Probe failed for {agent.name}: {e}")
        return agent

    # If get_adcp_capabilities is available, call it for rich discovery
    if "get_adcp_capabilities" in agent.tools:
        try:
            _apply_capabilities(agent, await get_adcp_capabilities_tool(agent))
        except SellerError as e:
            logger.debug(f"get_adcp_capabilities failed for {agent.name}: {e}")

    agent.apply_account_config()

    # Try fetching server card (best-effort)
    card = await fetch_server_card(agent.url)
    if card:
        agent.server_card = card
        adcp_meta = _extract_adcp_meta(card)
        if adcp_meta and not agent.supported_protocols:
            agent.supported_protocols = adcp_meta.get("protocols_supported", [])
            agent.extensions_supported = adcp_meta.get("extensions_supported", [])

    return agent


def _normalize_url(url: str) -> str:
    """Normalize URLs for dedup: strip trailing slash and /mcp suffix."""
    return url.rstrip("/").removesuffix("/mcp").lower()


async def discover_all_sellers(probe: bool = False) -> list[SellerAgent]:
    """Discover sellers from all sources: config + registry.

    Config sellers take priority (they have auth tokens).
    Registry sellers are added if not already in config.

    If probe=True, connects to each seller to discover tools and status.
    """
    sellers = get_configured_sellers()
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
        for s in sellers:
            s.apply_account_config()
        logger.info(
            f"Total sellers: {len(sellers)} ({sum(1 for s in sellers if s.token)} with auth)"
        )

    return sellers
