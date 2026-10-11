"""Live integration test against the AdCP test agent.

This connects to the real test agent at adcontextprotocol.org (read-only calls).
The agent's bearer token is read from NXFLO_SELLER_TOKEN_ADCP_TEST (the public
shared test token from the AdCP docs); tests that call the agent are skipped
when it is unset. Run with:
    NXFLO_SELLER_TOKEN_ADCP_TEST=... .venv/bin/python -m pytest tests/test_live_agent.py -v -s
"""

import asyncio
import os
import sys

import pytest

from src.connections.seller import (
    SellerError,
    call_seller_tool,
    get_seller_capabilities,
    get_seller_products,
)
from src.discovery.registry import SellerAgent, discover_all_sellers, fetch_registry_agents

TEST_TOKEN = os.environ.get("NXFLO_SELLER_TOKEN_ADCP_TEST")
requires_token = pytest.mark.skipif(
    not TEST_TOKEN, reason="NXFLO_SELLER_TOKEN_ADCP_TEST not set (live test agent token)"
)

TEST_AGENT = SellerAgent(
    name="AdCP Test Agent",
    url="https://test-agent.adcontextprotocol.org/mcp",
    token=TEST_TOKEN,
    source="config",
)


async def test_discover_registry():
    """Test fetching agents from the AdCP registry."""
    agents = await fetch_registry_agents()
    print(f"\nRegistry agents: {len(agents)}")
    for a in agents:
        print(f"  - {a.name} ({a.url}) [{a.status}]")
    assert len(agents) > 0, "Registry should have at least one agent"


async def test_discover_all():
    """Test full discovery (config + registry)."""
    sellers = await discover_all_sellers()
    print(f"\nAll sellers: {len(sellers)}")
    for s in sellers:
        print(f"  - {s.name} [source={s.source}, auth={'yes' if s.token else 'no'}]")
    assert len(sellers) >= 1, "Should have at least the test agent"


@requires_token
async def test_get_capabilities():
    """Test getting capabilities from the test agent."""
    caps = await get_seller_capabilities(TEST_AGENT)
    print(f"\nCapabilities: {caps}")
    assert caps, "Should return capabilities"


@requires_token
async def test_get_products():
    """Test getting products from the test agent."""
    products = await get_seller_products(
        TEST_AGENT,
        brief="Premium athletic footwear advertising",
        brand_name="TestBrand",
        brand_url="https://testbrand.com",
    )
    print(f"\nProducts response: {products}")
    assert products, "Should return a response"

    if "products" in products:
        print(f"Found {len(products['products'])} products")
        for p in products["products"][:3]:
            print(f"  - {p.get('name', 'unnamed')}: {p.get('product_id', 'no-id')}")


@requires_token
async def test_list_creative_formats():
    """Test listing creative formats from the test agent."""
    formats = await call_seller_tool(TEST_AGENT, "list_creative_formats", {})
    print(f"\nCreative formats: {formats}")


async def main():
    """Run all tests manually (no pytest needed)."""
    print("=" * 60)
    print("Nexflo Buyer - Live Integration Test")
    print("=" * 60)

    tests = [
        ("Registry Discovery", test_discover_registry),
        ("Full Discovery", test_discover_all),
        ("Get Capabilities", test_get_capabilities),
        ("Get Products", test_get_products),
        ("List Creative Formats", test_list_creative_formats),
    ]

    passed = 0
    failed = 0

    for name, test_fn in tests:
        print(f"\n--- {name} ---")
        try:
            await test_fn()
            print("  PASS")
            passed += 1
        except (AssertionError, SellerError) as e:
            print(f"  FAIL: {e}")
            failed += 1

    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
