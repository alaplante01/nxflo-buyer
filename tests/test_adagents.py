"""Sales-agent authorization against publisher adagents.json (fail closed)."""

from datetime import UTC, datetime
from typing import Any

import pytest

from src.authorization.adagents import SalesAgentAuthorizer, SellerNotAuthorizedError
from src.protocol.safe_fetch import FetchError, FetchPolicy, RedirectRule

SELLER = "https://sales.example/mcp"
PUB = "news.example"
WK = f"https://{PUB}/.well-known/adagents.json"
PROPS = [
    {"property_id": "home", "property_type": "website", "name": "Home", "identifiers": [{"type": "domain", "value": PUB}], "tags": ["premium"]},
    {"property_id": "sports", "property_type": "website", "name": "Sports", "identifiers": [{"type": "domain", "value": f"sports.{PUB}"}], "tags": ["premium", "sports"]},
]


class FakeFetcher:
    def __init__(self, docs: dict[str, Any]) -> None:
        self.docs = docs
        self.calls: list[tuple[str, RedirectRule]] = []

    async def __call__(self, url: str, policy: FetchPolicy) -> Any:
        self.calls.append((url, policy.redirect_rule))
        if url not in self.docs:
            raise FetchError(f"GET {url} returned HTTP 404", status_code=404)
        return self.docs[url]


def adagents(*agents: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"properties": PROPS, "authorized_agents": list(agents), **extra}


def product(selection: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    selector = {"publisher_domain": PUB, "selection_type": "all"} if selection is None else selection
    return {"product_id": "p1", "publisher_properties": [selector], **extra}


def authorizer(docs: dict[str, Any]) -> tuple[SalesAgentAuthorizer, FakeFetcher]:
    fetcher = FakeFetcher(docs)
    return SalesAgentAuthorizer(fetcher, now=lambda: datetime(2026, 10, 11, tzinfo=UTC)), fetcher


@pytest.mark.parametrize(
    "agent",
    [
        {"url": SELLER, "authorization_type": "property_ids", "property_ids": ["home", "sports"]},
        {"url": SELLER, "authorization_type": "property_tags", "property_tags": ["premium"]},
        {"url": SELLER, "authorization_type": "publisher_properties",
         "publisher_properties": [{"publisher_domain": PUB, "selection_type": "all"}]},
        {"url": "HTTPS://Sales.Example:443/mcp", "authorization_type": "property_tags", "property_tags": ["premium"]},
    ],
)
async def test_authorized_patterns(agent: dict[str, Any]) -> None:
    auth, _ = authorizer({WK: adagents(agent)})
    await auth.verify_product(SELLER, product())


async def test_by_tag_selector_and_inline_properties() -> None:
    inline = {"url": SELLER, "authorization_type": "inline_properties", "properties": [PROPS[1]]}
    auth, _ = authorizer({WK: adagents(inline)})
    await auth.verify_product(SELLER, product({"publisher_domain": PUB, "selection_type": "by_tag", "property_tags": ["sports"]}))
    with pytest.raises(SellerNotAuthorizedError):
        await auth.verify_product(SELLER, product())  # "all" also claims "home"


async def test_partial_grant_fails() -> None:
    auth, _ = authorizer({WK: adagents({"url": SELLER, "authorization_type": "property_ids", "property_ids": ["home"]})})
    with pytest.raises(SellerNotAuthorizedError) as info:
        await auth.verify_product(SELLER, product())
    assert "sports" in str(info.value)


@pytest.mark.parametrize(
    "agent_url",
    ["https://sales.example", "https://sales.example/mcp/", "http://sales.example/mcp", "https://other.example/mcp"],
)
async def test_agent_url_must_match_canonically_including_path(agent_url: str) -> None:
    auth, _ = authorizer({WK: adagents({"url": agent_url, "authorization_type": "property_tags", "property_tags": ["premium"]})})
    with pytest.raises(SellerNotAuthorizedError):
        await auth.verify_product(SELLER, product())


async def test_missing_file_fails_closed_and_is_cached() -> None:
    auth, fetcher = authorizer({})
    for _ in range(2):
        with pytest.raises(SellerNotAuthorizedError) as info:
            await auth.verify_product(SELLER, product())
        assert "unavailable" in info.value.reason
    assert len(fetcher.calls) == 1


async def test_product_without_publisher_properties_fails_closed() -> None:
    auth, _ = authorizer({})
    with pytest.raises(SellerNotAuthorizedError):
        await auth.verify_product(SELLER, {"product_id": "p1"})
    with pytest.raises(SellerNotAuthorizedError):
        await auth.verify_product(SELLER, product({"publisher_domains": [PUB], "selection_type": "all"}))


@pytest.mark.parametrize(
    ("extra", "countries"),
    [
        ({"effective_until": "2026-01-01T00:00:00Z"}, None),
        ({"effective_from": "2027-01-01T00:00:00Z"}, None),
        ({"countries": ["US"]}, None),
        ({"countries": ["US"]}, ["DE"]),
        ({"collections": [{"publisher_domain": PUB}]}, None),
        ({"placement_ids": ["homepage_top"]}, None),
    ],
)
async def test_out_of_scope_grants_fail(extra: dict[str, Any], countries: list[str] | None) -> None:
    agent = {"url": SELLER, "authorization_type": "property_tags", "property_tags": ["premium"], **extra}
    auth, _ = authorizer({WK: adagents(agent)})
    with pytest.raises(SellerNotAuthorizedError):
        await auth.verify_product(SELLER, product(), countries=countries)


async def test_in_scope_country_and_placement_grant_passes() -> None:
    agent = {"url": SELLER, "authorization_type": "property_tags", "property_tags": ["premium"],
             "countries": ["US", "CA"], "placement_ids": ["homepage_top"]}
    auth, _ = authorizer({WK: adagents(agent)})
    await auth.verify_product(SELLER, product(placements=[{"placement_id": "homepage_top"}]), countries=["US"])


async def test_revoked_publisher_domain_fails() -> None:
    agent = {"url": SELLER, "authorization_type": "property_tags", "property_tags": ["premium"]}
    doc = adagents(agent, revoked_publisher_domains=[{"publisher_domain": PUB, "revoked_at": "2026-09-01T00:00:00Z"}])
    auth, _ = authorizer({WK: doc})
    with pytest.raises(SellerNotAuthorizedError):
        await auth.verify_product(SELLER, product())


async def test_unknown_property_id_fails() -> None:
    agent = {"url": SELLER, "authorization_type": "property_ids", "property_ids": ["home", "ghost"]}
    auth, _ = authorizer({WK: adagents(agent)})
    with pytest.raises(SellerNotAuthorizedError):
        await auth.verify_product(SELLER, product({"publisher_domain": PUB, "selection_type": "by_id", "property_ids": ["ghost"]}))


async def test_authoritative_location_single_hop_only() -> None:
    hosted = "https://cdn.network.example/adagents.json"
    agent = {"url": SELLER, "authorization_type": "property_tags", "property_tags": ["premium"]}
    auth, fetcher = authorizer({WK: {"authoritative_location": hosted}, hosted: adagents(agent)})
    await auth.verify_product(SELLER, product())
    assert (hosted, RedirectRule.NONE) in fetcher.calls
    assert (WK, RedirectRule.SAME_SITE) in fetcher.calls

    nested, _ = authorizer({WK: {"authoritative_location": hosted}, hosted: {"authoritative_location": "https://x.example/a.json"}})
    with pytest.raises(SellerNotAuthorizedError):
        await nested.verify_product(SELLER, product())


async def test_malformed_property_is_skipped_not_fatal() -> None:
    agent = {"url": SELLER, "authorization_type": "property_ids", "property_ids": ["home"]}
    doc = {"properties": [PROPS[0], {"name": "broken"}], "authorized_agents": [agent]}
    auth, _ = authorizer({WK: doc})
    await auth.verify_product(SELLER, product({"publisher_domain": PUB, "selection_type": "by_id", "property_ids": ["home"]}))
