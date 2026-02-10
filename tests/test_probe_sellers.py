"""Probe all configured + registry sellers for connectivity.

Tests which sellers accept unauthenticated MCP connections and
what tools they expose. Run with:
    .venv/Scripts/python -m tests.test_probe_sellers
"""

import asyncio
import sys
import logging

from src.discovery.registry import discover_all_sellers
from src.connections.seller import connect_to_seller, SellerConnectionError

logging.basicConfig(level=logging.WARNING)


async def probe_seller(agent):
    """Try to connect and list tools on a seller."""
    try:
        async with connect_to_seller(agent) as client:
            tools = await client.list_tools()
            tool_names = [t.name for t in tools] if tools else []
            return {"status": "ok", "tools": tool_names}
    except SellerConnectionError as e:
        return {"status": "error", "error": str(e)}
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


async def main():
    print("=" * 70)
    print("Nexflo Buyer — Seller Probe")
    print("=" * 70)

    sellers = await discover_all_sellers()
    print(f"\nFound {len(sellers)} sellers ({sum(1 for s in sellers if s.token)} with auth)\n")

    results = []
    for seller in sellers:
        auth_tag = "AUTH" if seller.token else "NO-AUTH"
        print(f"  Probing {seller.name:30s} [{auth_tag:7s}] {seller.url}")
        result = await probe_seller(seller)

        if result["status"] == "ok":
            print(f"    -> OK ({len(result['tools'])} tools: {', '.join(result['tools'][:5])})")
            results.append((seller, result))
        else:
            err = result["error"][:80]
            print(f"    -> FAIL: {err}")
            results.append((seller, result))

    print(f"\n{'=' * 70}")
    ok = sum(1 for _, r in results if r["status"] == "ok")
    fail = sum(1 for _, r in results if r["status"] != "ok")
    print(f"Results: {ok} accessible, {fail} failed, {len(results)} total")

    if ok > 0:
        print(f"\nAccessible sellers:")
        for seller, r in results:
            if r["status"] == "ok":
                print(f"  - {seller.name}: {len(r['tools'])} tools")

    return ok > 0


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
