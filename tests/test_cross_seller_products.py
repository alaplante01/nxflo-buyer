"""Cross-seller product comparison test.

Queries get_products across all accessible sellers and compares results.
Run with: .venv/Scripts/python -m tests.test_cross_seller_products
"""

import asyncio
import json
import sys
import logging

from src.discovery.registry import discover_all_sellers
from src.connections.seller import get_seller_products, get_seller_capabilities, SellerConnectionError

logging.basicConfig(level=logging.WARNING)

BRIEF = "Premium digital advertising for athletic footwear brand targeting sports enthusiasts"
BRAND = "TestBrand"
BRAND_URL = "https://testbrand.example.com"


async def query_products(agent):
    """Query products from a single seller."""
    try:
        # First check if seller has get_products
        caps = await get_seller_capabilities(agent)
        if "get_products" not in caps.get("tools", []):
            return {"status": "no_get_products", "tools": caps.get("tools", [])}

        products = await get_seller_products(agent, BRIEF, BRAND, BRAND_URL)
        return {"status": "ok", "response": products}
    except SellerConnectionError as e:
        return {"status": "connection_error", "error": str(e)}
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


async def main():
    print("=" * 70)
    print("Nexflo Buyer — Cross-Seller Product Comparison")
    print("=" * 70)
    print(f"\nBrief: {BRIEF}")
    print(f"Brand: {BRAND} ({BRAND_URL})\n")

    sellers = await discover_all_sellers()

    # Filter to sellers that are likely to have products
    results = []
    for seller in sellers:
        print(f"  Querying {seller.name:30s} ...", end=" ", flush=True)
        result = await query_products(seller)

        if result["status"] == "ok":
            resp = result["response"]
            # Count products
            if isinstance(resp, dict) and "products" in resp:
                count = len(resp["products"])
                print(f"-> {count} products")
                results.append((seller, resp))
            elif isinstance(resp, dict) and "raw" in resp:
                text = resp["raw"][:100]
                print(f"-> text response: {text}...")
                results.append((seller, resp))
            else:
                print(f"-> response: {str(resp)[:80]}")
                results.append((seller, resp))
        elif result["status"] == "no_get_products":
            tools = result.get("tools", [])
            print(f"-> no get_products (has: {', '.join(tools[:3])})")
        else:
            err = result.get("error", "unknown")[:60]
            print(f"-> FAIL: {err}")

    print(f"\n{'=' * 70}")
    print(f"Sellers with products: {len(results)}")

    # Detailed product listing
    for seller, resp in results:
        print(f"\n--- {seller.name} ---")
        if isinstance(resp, dict) and "products" in resp:
            for p in resp["products"][:3]:
                name = p.get("name", "unnamed")
                pid = p.get("product_id", "no-id")
                desc = p.get("description", "")[:80]
                # Extract pricing
                price = "no price"
                for pricing in p.get("pricing_options", []):
                    model = pricing.get("pricing_model", "")
                    rate = pricing.get("rate", "?")
                    currency = pricing.get("currency", "USD")
                    price = f"{model} {currency} {rate}"
                    break
                print(f"  [{pid}] {name}")
                print(f"    Price: {price}")
                if desc:
                    print(f"    Desc: {desc}")
            total = len(resp["products"])
            if total > 3:
                print(f"  ... and {total - 3} more")
        elif isinstance(resp, dict) and "raw" in resp:
            print(f"  Raw: {resp['raw'][:200]}")
        else:
            print(f"  Response: {json.dumps(resp, indent=2)[:200]}")


if __name__ == "__main__":
    asyncio.run(main())
