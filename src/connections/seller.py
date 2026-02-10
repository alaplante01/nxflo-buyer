"""MCP client wrapper for connecting to seller agents.

Adapted from the Prebid Sales Agent's mcp_client.py pattern:
- StreamableHttpTransport for HTTP/SSE
- Retry with exponential backoff
- /mcp fallback if URL doesn't end with it
"""

import json
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastmcp.client import Client
from fastmcp.client.transports import StreamableHttpTransport

from src.discovery.registry import SellerAgent

logger = logging.getLogger(__name__)


class SellerConnectionError(Exception):
    """Failed to connect to a seller agent after all retries."""


def _build_headers(agent: SellerAgent) -> dict[str, str]:
    """Build auth headers for a seller agent."""
    headers: dict[str, str] = {}
    if agent.token:
        headers["Authorization"] = f"Bearer {agent.token}"
    return headers


@asynccontextmanager
async def connect_to_seller(agent: SellerAgent):
    """Connect to a seller agent via MCP.

    Yields a connected MCP Client.

    Usage:
        async with connect_to_seller(agent) as client:
            result = await client.call_tool("get_products", {...})
    """
    url = agent.url.rstrip("/")
    headers = _build_headers(agent)

    transport = StreamableHttpTransport(url=url, headers=headers)
    client = Client(transport=transport)

    try:
        async with client:
            logger.debug(f"Connected to {agent.name} at {url}")
            yield client
    except Exception as e:
        raise SellerConnectionError(
            f"Failed to connect to {agent.name} at {url}: {e}"
        ) from e


async def call_seller_tool(
    agent: SellerAgent, tool_name: str, params: dict[str, Any]
) -> dict[str, Any]:
    """Call a tool on a seller agent and return the parsed response.

    This is the main interface for interacting with sellers.
    Handles connection, tool invocation, and response parsing.
    """
    async with connect_to_seller(agent) as client:
        result = await client.call_tool(tool_name, params)

        # FastMCP returns CallToolResult with content list
        # Each content item has .text for text content
        if hasattr(result, "content") and result.content:
            for item in result.content:
                if hasattr(item, "text") and item.text:
                    try:
                        return json.loads(item.text)
                    except json.JSONDecodeError:
                        return {"raw": item.text}

        # Fallback: try structured_content
        if hasattr(result, "structured_content") and result.structured_content:
            return result.structured_content

        return {"status": "empty_response"}


async def get_seller_capabilities(agent: SellerAgent) -> dict[str, Any]:
    """Get a seller's capabilities by listing available tools.

    Not all agents implement get_adcp_capabilities, so we fall back
    to listing tools as a discovery mechanism.
    """
    async with connect_to_seller(agent) as client:
        tools = await client.list_tools()
        tool_names = [t.name for t in tools] if tools else []
        return {
            "agent": agent.name,
            "url": agent.url,
            "tools": tool_names,
            "tool_count": len(tool_names),
        }


async def get_seller_products(
    agent: SellerAgent, brief: str, brand_name: str = "Nexflo", brand_url: str = "https://nexflo.io"
) -> dict[str, Any]:
    """Get products from a seller matching a brief."""
    return await call_seller_tool(
        agent,
        "get_products",
        {
            "brief": brief,
            "brand_manifest": {"name": brand_name, "url": brand_url},
        },
    )


async def create_media_buy_on_seller(
    agent: SellerAgent,
    product_id: str,
    budget: float,
    buyer_ref: str,
    brand_manifest: dict[str, Any],
    pricing_option_id: str = "cpm-standard",
    start_time: str = "asap",
    end_time: str | None = None,
    push_notification_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a media buy on a seller agent.

    Matches the AdCP create_media_buy schema:
    - packages[].budget is a flat number (not {amount, currency})
    - packages[].buyer_ref and pricing_option_id are required per package
    - start_time is a string: "asap" or ISO 8601 datetime
    - end_time is a required ISO 8601 datetime string
    """
    from datetime import UTC, datetime, timedelta

    # Default end_time to 30 days from now if not specified
    if not end_time:
        end_time = (datetime.now(UTC) + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")

    params: dict[str, Any] = {
        "packages": [{
            "product_id": product_id,
            "budget": budget,
            "buyer_ref": buyer_ref,
            "pricing_option_id": pricing_option_id,
        }],
        "buyer_ref": buyer_ref,
        "brand_manifest": brand_manifest,
        "start_time": start_time,
        "end_time": end_time,
    }
    if push_notification_config:
        params["push_notification_config"] = push_notification_config

    return await call_seller_tool(agent, "create_media_buy", params)


async def get_delivery(agent: SellerAgent, media_buy_id: str) -> dict[str, Any]:
    """Get delivery metrics for a media buy."""
    return await call_seller_tool(
        agent, "get_media_buy_delivery", {"media_buy_ids": [media_buy_id]}
    )


# --- AdCP Protocol Tools (Phase 2) ---


async def get_adcp_capabilities_tool(
    agent: SellerAgent, protocols: list[str] | None = None
) -> dict[str, Any]:
    """Call get_adcp_capabilities on a seller to discover protocol support.

    This is the protocol-recommended way to discover what a seller supports,
    rather than just listing MCP tools.
    """
    params: dict[str, Any] = {}
    if protocols:
        params["protocols"] = protocols
    return await call_seller_tool(agent, "get_adcp_capabilities", params)


async def update_media_buy(
    agent: SellerAgent,
    media_buy_id: str | None = None,
    buyer_ref: str | None = None,
    paused: bool | None = None,
    end_time: str | None = None,
    packages: list[dict] | None = None,
) -> dict[str, Any]:
    """Update an existing media buy using PATCH semantics."""
    params: dict[str, Any] = {}
    if media_buy_id:
        params["media_buy_id"] = media_buy_id
    if buyer_ref:
        params["buyer_ref"] = buyer_ref
    if paused is not None:
        params["paused"] = paused
    if end_time:
        params["end_time"] = end_time
    if packages:
        params["packages"] = packages
    return await call_seller_tool(agent, "update_media_buy", params)


async def sync_creatives(
    agent: SellerAgent,
    creatives: list[dict],
    media_buy_id: str | None = None,
) -> dict[str, Any]:
    """Upload/sync creative assets for a media buy."""
    params: dict[str, Any] = {"creatives": creatives}
    if media_buy_id:
        params["media_buy_id"] = media_buy_id
    return await call_seller_tool(agent, "sync_creatives", params)


async def provide_performance_feedback(
    agent: SellerAgent,
    media_buy_id: str,
    performance_index: float,
    measurement_period: dict[str, Any],
) -> dict[str, Any]:
    """Share performance outcomes with a seller."""
    return await call_seller_tool(
        agent,
        "provide_performance_feedback",
        {
            "media_buy_id": media_buy_id,
            "performance_index": performance_index,
            "measurement_period": measurement_period,
        },
    )


async def get_signals(
    agent: SellerAgent,
    brief: str,
    platforms: list[dict] | None = None,
) -> dict[str, Any]:
    """Discover audience signals from a signals agent.

    Dstillery (and potentially others) expect signal_spec, not brief.
    """
    params: dict[str, Any] = {"signal_spec": brief}
    if platforms:
        params["platforms"] = platforms
    return await call_seller_tool(agent, "get_signals", params)


async def activate_signal(
    agent: SellerAgent,
    signal_id: str,
    platform: dict[str, Any],
) -> dict[str, Any]:
    """Activate a signal for use in campaigns."""
    return await call_seller_tool(
        agent,
        "activate_signal",
        {"signal_id": signal_id, "platform": platform},
    )


async def tasks_get(
    agent: SellerAgent,
    task_id: str,
    include_result: bool = True,
) -> dict[str, Any]:
    """Poll a specific task status using the standard tasks/get tool."""
    return await call_seller_tool(
        agent,
        "tasks/get",
        {"task_id": task_id, "include_result": include_result},
    )


async def tasks_list(
    agent: SellerAgent,
    filters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """List tasks on a seller using the standard tasks/list tool."""
    params: dict[str, Any] = {}
    if filters:
        params["filters"] = filters
    return await call_seller_tool(agent, "tasks/list", params)
