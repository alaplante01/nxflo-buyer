"""Extra probes for Content Ignite and Dstillery signals."""

import asyncio
import json

from src.discovery.registry import SellerAgent
from src.connections.seller import connect_to_seller, call_seller_tool


async def main():
    # Test Content Ignite with /mcp suffix
    ci = SellerAgent(name="Content Ignite", url="https://sales-agent.contentignite.com/mcp", source="test")
    try:
        async with connect_to_seller(ci) as client:
            tools = await client.list_tools()
            print(f"Content Ignite /mcp: {len(tools)} tools - {[t.name for t in tools]}")
    except Exception as e:
        print(f"Content Ignite /mcp: FAIL - {e}")

    # Test Dstillery signals
    dst = SellerAgent(name="Dstillery", url="https://adcp-signals-agent.dstillery.com/mcp", source="test")
    try:
        result = await call_seller_tool(dst, "get_signals", {"brief": "sports enthusiasts athletic footwear"})
        print(f"Dstillery signals: {json.dumps(result, indent=2)[:500]}")
    except Exception as e:
        print(f"Dstillery signals: FAIL - {e}")

    # Test Bidcliq get_adcp_capabilities (it has this tool)
    bc = SellerAgent(name="Bidcliq", url="https://agents.bidcliq.com/mcp", source="test")
    try:
        result = await call_seller_tool(bc, "get_adcp_capabilities", {})
        print(f"Bidcliq capabilities: {json.dumps(result, indent=2)[:500]}")
    except Exception as e:
        print(f"Bidcliq capabilities: FAIL - {e}")


if __name__ == "__main__":
    asyncio.run(main())
