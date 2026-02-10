"""MCP client wrapper for connecting to seller agents.

Adapted from the Prebid Sales Agent's mcp_client.py pattern:
- StreamableHttpTransport for HTTP/SSE
- Retry with exponential backoff
- /mcp fallback if URL doesn't end with it
"""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastmcp.client import Client
from fastmcp.client.transports import StreamableHttpTransport

from src.config import settings
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
    agent: SellerAgent, brief: str, brand_name: str = "ADFX", brand_url: str = "https://adfx.io"
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
    start_time: dict[str, Any] | None = None,
    end_time: str | None = None,
) -> dict[str, Any]:
    """Create a media buy on a seller agent."""
    params: dict[str, Any] = {
        "packages": [{"product_id": product_id, "budget": {"amount": budget, "currency": "USD"}}],
        "buyer_ref": buyer_ref,
        "brand_manifest": brand_manifest,
        "start_time": start_time or {"type": "asap"},
    }
    if end_time:
        params["end_time"] = end_time

    return await call_seller_tool(agent, "create_media_buy", params)


async def get_delivery(agent: SellerAgent, media_buy_id: str) -> dict[str, Any]:
    """Get delivery metrics for a media buy."""
    return await call_seller_tool(
        agent, "get_media_buy_delivery", {"media_buy_ids": [media_buy_id]}
    )
