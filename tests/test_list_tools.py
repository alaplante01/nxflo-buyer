"""List all tools available on the test agent.

Reads the bearer token from NXFLO_SELLER_TOKEN_ADCP_TEST (never hardcoded).
"""

import asyncio
import os

from fastmcp.client import Client
from fastmcp.client.transports import StreamableHttpTransport


async def main():
    url = "https://test-agent.adcontextprotocol.org/mcp"
    token = os.environ.get("NXFLO_SELLER_TOKEN_ADCP_TEST", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    transport = StreamableHttpTransport(url=url, headers=headers)
    client = Client(transport=transport)

    async with client:
        tools = await client.list_tools()
        print(f"Found {len(tools)} tools:")
        for t in tools:
            print(f"  - {t.name}: {t.description[:80] if t.description else 'no desc'}")


if __name__ == "__main__":
    asyncio.run(main())
