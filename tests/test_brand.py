"""Brand Protocol: brand.json resolution, brand refs, authorized_operators (fail closed)."""

from datetime import UTC, datetime
from typing import Any

import pytest

from src.brand.resolver import (
    BrandAuthorizationError,
    BrandAuthorizationResolver,
    build_brand_ref,
    domain_from_url,
)
from src.protocol.safe_fetch import FetchError, FetchPolicy, RedirectRule

OPERATOR = "nxflo.io"


class FakeFetcher:
    """Serves brand.json documents by URL; records calls and policies."""

    def __init__(self, docs: dict[str, Any]) -> None:
        self.docs = docs
        self.calls: list[tuple[str, RedirectRule]] = []

    async def __call__(self, url: str, policy: FetchPolicy) -> Any:
        self.calls.append((url, policy.redirect_rule))
        if url not in self.docs:
            raise FetchError(f"GET {url} returned HTTP 404", status_code=404)
        return self.docs[url]


def wk(domain: str) -> str:
    return f"https://{domain}/.well-known/brand.json"


def portfolio(operators: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {
        "house": {"domain": "acme.example", "name": "Acme"},
        "brands": [
            {"id": "acme", "names": [{"en": "Acme"}], "properties": [{"type": "website", "identifier": "acme.example"}]},
            {"id": "rocket", "names": [{"en": "Rocket"}], "properties": [{"type": "website", "identifier": "rocket.example"}]},
        ],
        "authorized_operators": operators,
        **extra,
    }


def resolver(docs: dict[str, Any], clock: list[float] | None = None) -> tuple[BrandAuthorizationResolver, FakeFetcher]:
    fetcher = FakeFetcher(docs)
    ticks = clock if clock is not None else [0.0]
    res = BrandAuthorizationResolver(
        fetcher, ttl_seconds=100, negative_ttl_seconds=10, clock=lambda: ticks[0],
        now=lambda: datetime(2026, 10, 11, tzinfo=UTC),
    )
    return res, fetcher


def test_build_brand_ref_and_domain_from_url() -> None:
    assert build_brand_ref("Acme.Example.", "acme", ["us", "DE", "us"]) == {
        "domain": "acme.example", "brand_id": "acme", "countries": ["DE", "US"],
    }
    assert build_brand_ref("acme.example") == {"domain": "acme.example"}
    assert domain_from_url("https://www.Acme.example/path") == "acme.example"


async def test_authorized_operator_in_house_portfolio() -> None:
    res, _ = resolver({wk("acme.example"): portfolio([{"domain": OPERATOR, "brands": ["acme"]}])})
    resolution = await res.verify_operator({"domain": "acme.example"}, OPERATOR)
    assert resolution.brand_id == "acme" and resolution.kind == "house_portfolio"


async def test_wildcard_brands_and_brand_id_selection() -> None:
    res, _ = resolver({wk("acme.example"): portfolio([{"domain": OPERATOR, "brands": ["*"]}])})
    resolution = await res.verify_operator({"domain": "acme.example", "brand_id": "rocket"}, OPERATOR)
    assert resolution.brand_id == "rocket"


async def test_operator_not_listed_fails_closed() -> None:
    res, _ = resolver({wk("acme.example"): portfolio([{"domain": "other-agency.example", "brands": ["*"]}])})
    with pytest.raises(BrandAuthorizationError) as info:
        await res.verify_operator({"domain": "acme.example"}, OPERATOR)
    assert info.value.code == "operator_not_authorized"


async def test_operator_listed_for_other_brand_fails() -> None:
    res, _ = resolver({wk("rocket.example"): {"house": "acme.example"}, wk("acme.example"): portfolio([{"domain": OPERATOR, "brands": ["acme"]}])})
    with pytest.raises(BrandAuthorizationError):
        await res.verify_operator({"domain": "rocket.example"}, OPERATOR)


@pytest.mark.parametrize(
    "entry",
    [
        {"domain": OPERATOR, "brands": ["*"], "valid_until": "2026-01-01T00:00:00Z"},
        {"domain": OPERATOR, "brands": ["*"], "valid_from": "2027-01-01T00:00:00Z"},
        {"domain": OPERATOR, "brands": ["*"], "scopes": ["creative_generation"]},
        {"domain": OPERATOR, "brands": ["*"], "countries": ["DE"]},
    ],
)
async def test_scoped_or_expired_entries_do_not_authorize(entry: dict[str, Any]) -> None:
    res, _ = resolver({wk("acme.example"): portfolio([entry])})
    with pytest.raises(BrandAuthorizationError):
        await res.verify_operator({"domain": "acme.example"}, OPERATOR)


async def test_country_and_scope_match_authorizes() -> None:
    entry = {"domain": OPERATOR, "brands": ["acme"], "countries": ["US", "CA"], "scopes": ["media_buying"]}
    res, _ = resolver({wk("acme.example"): portfolio([entry])})
    await res.verify_operator({"domain": "acme.example", "countries": ["US"]}, OPERATOR)


async def test_missing_brand_json_fails_closed_and_is_negative_cached() -> None:
    clock = [0.0]
    res, fetcher = resolver({}, clock)
    for _ in range(2):
        with pytest.raises(BrandAuthorizationError) as info:
            await res.verify_operator({"domain": "nobrand.example"}, OPERATOR)
        assert info.value.code == "brand_json_unavailable"
    assert len(fetcher.calls) == 1
    clock[0] = 11.0
    with pytest.raises(BrandAuthorizationError):
        await res.verify_operator({"domain": "nobrand.example"}, OPERATOR)
    assert len(fetcher.calls) == 2


async def test_positive_cache_ttl() -> None:
    clock = [0.0]
    res, fetcher = resolver({wk("acme.example"): portfolio([{"domain": OPERATOR, "brands": ["*"]}])}, clock)
    await res.verify_operator({"domain": "acme.example"}, OPERATOR)
    await res.verify_operator({"domain": "acme.example"}, OPERATOR)
    assert len(fetcher.calls) == 1
    clock[0] = 101.0
    await res.verify_operator({"domain": "acme.example"}, OPERATOR)
    assert len(fetcher.calls) == 2


async def test_house_redirect_and_authoritative_location() -> None:
    hosted = "https://registry.example/brands/acme/brand.json"
    house = portfolio([{"domain": OPERATOR, "brands": ["acme"]}])
    house["brands"][0]["properties"].append({"type": "website", "identifier": "acme-regional.example", "region": "CA"})
    docs = {
        wk("acme-regional.example"): {"house": "acme.example", "region": "CA"},
        wk("acme.example"): {"authoritative_location": hosted},
        hosted: house,
    }
    res, fetcher = resolver(docs)
    resolution = await res.verify_operator({"domain": "acme-regional.example"}, OPERATOR)
    assert resolution.brand_id == "acme"
    assert (hosted, RedirectRule.NONE) in fetcher.calls
    assert (wk("acme.example"), RedirectRule.WWW_ONLY) in fetcher.calls


async def test_redirect_to_house_that_does_not_list_the_domain_fails_closed() -> None:
    docs = {
        wk("impostor.example"): {"house": "acme.example"},
        wk("acme.example"): portfolio([{"domain": OPERATOR, "brands": ["*"]}]),
    }
    res, _ = resolver(docs)
    with pytest.raises(BrandAuthorizationError) as info:
        await res.verify_operator({"domain": "impostor.example"}, OPERATOR)
    assert info.value.code == "relationship_unverified"


async def test_redirect_loop_is_bounded() -> None:
    docs = {wk("a.example"): {"house": "b.example"}, wk("b.example"): {"house": "a.example"}}
    res, _ = resolver(docs)
    with pytest.raises(BrandAuthorizationError) as info:
        await res.verify_operator({"domain": "a.example"}, OPERATOR)
    assert info.value.code == "brand_json_redirect_loop"


async def test_standalone_canonical_document_cannot_authorize_third_parties() -> None:
    res, _ = resolver({wk("solo.example"): {"id": "solo", "names": [{"en": "Solo"}]}})
    with pytest.raises(BrandAuthorizationError) as info:
        await res.verify_operator({"domain": "solo.example"}, OPERATOR)
    assert info.value.code == "operator_not_authorized"


async def test_canonical_leaf_uses_house_operators_only_when_reciprocated() -> None:
    leaf = {"id": "converse", "names": [{"en": "Converse"}], "house_domain": "nikeinc.example"}
    house = {
        "house": {"domain": "nikeinc.example", "name": "Nike"},
        "brand_refs": [{"domain": "converse.example", "brand_id": "converse"}],
        "authorized_operators": [{"domain": OPERATOR, "brands": ["converse"]}],
    }
    res, _ = resolver({wk("converse.example"): leaf, wk("nikeinc.example"): house})
    resolution = await res.verify_operator({"domain": "converse.example"}, OPERATOR)
    assert resolution.relationship_verified and resolution.house_domain == "nikeinc.example"

    unreciprocated = {**house, "brand_refs": []}
    res, _ = resolver({wk("converse.example"): leaf, wk("nikeinc.example"): unreciprocated})
    with pytest.raises(BrandAuthorizationError) as info:
        await res.verify_operator({"domain": "converse.example"}, OPERATOR)
    assert info.value.code == "relationship_unverified"


async def test_brand_agent_document_fails_closed() -> None:
    res, _ = resolver({wk("agent.example"): {"agents": [{"type": "brand", "url": "https://a.example/mcp", "id": "a"}]}})
    with pytest.raises(BrandAuthorizationError) as info:
        await res.verify_operator({"domain": "agent.example"}, OPERATOR)
    assert info.value.code == "brand_agent_only"


async def test_self_operation_needs_no_listing() -> None:
    res, fetcher = resolver({})
    resolution = await res.verify_operator({"domain": "nxflo.io"}, OPERATOR)
    assert resolution.kind == "self" and fetcher.calls == []


async def test_unknown_brand_id_rejected() -> None:
    res, _ = resolver({wk("acme.example"): portfolio([{"domain": OPERATOR, "brands": ["*"]}])})
    with pytest.raises(BrandAuthorizationError) as info:
        await res.verify_operator({"domain": "acme.example", "brand_id": "ghost"}, OPERATOR)
    assert info.value.code == "brand_not_in_portfolio"
