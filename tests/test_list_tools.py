"""List all tools available on the test agent."""

import asyncio
from fastmcp.client import Client
from fastmcp.client.transports import StreamableHttpTransport


async def main():
    url = "https://test-agent.adcontextprotocol.org/mcp"
    headers = {"Authorization": "Bearer 1v8tAhASaUYYp4odoQ1PnMpdqNaMiTrCRqYo9OJp6IQ"}
    transport = StreamableHttpTransport(url=url, headers=headers)
    client = Client(transport=transport)

    async with client:
        tools = await client.list_tools()
        print(f"Found {len(tools)} tools:")
        for t in tools:
            print(f"  - {t.name}: {t.description[:80] if t.description else 'no desc'}")


if __name__ == "__main__":
    asyncio.run(main())
