"""SSRF-aware trust-root fetcher: HTTPS only, public addresses, redirect policy, strict JSON."""

import json

import httpx
import pytest

from src.protocol.safe_fetch import (
    FetchError,
    FetchPolicy,
    RedirectRule,
    fetch_json,
    parse_strict_json,
)


async def public_resolver(host: str, port: int) -> list[str]:
    return ["93.184.216.34"]


async def private_resolver(host: str, port: int) -> list[str]:
    return ["10.0.0.7"]


def _client(routes: dict[str, httpx.Response]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return routes.get(str(request.url), httpx.Response(404))

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


WWW = FetchPolicy(max_bytes=1024, redirect_rule=RedirectRule.WWW_ONLY)
NONE = FetchPolicy(max_bytes=1024, redirect_rule=RedirectRule.NONE)
SITE = FetchPolicy(max_bytes=1024, redirect_rule=RedirectRule.SAME_SITE)
URL = "https://brand.example/.well-known/brand.json"


async def test_fetches_and_parses_json() -> None:
    async with _client({URL: httpx.Response(200, json={"a": 1})}) as client:
        assert await fetch_json(URL, WWW, client=client, resolver=public_resolver) == {"a": 1}


async def test_404_is_reported_with_status() -> None:
    async with _client({}) as client:
        with pytest.raises(FetchError) as info:
            await fetch_json(URL, WWW, client=client, resolver=public_resolver)
    assert info.value.not_found


async def test_www_redirect_allowed_other_host_refused() -> None:
    www = "https://www.brand.example/.well-known/brand.json"
    routes = {URL: httpx.Response(301, headers={"location": www}), www: httpx.Response(200, json={"ok": True})}
    async with _client(routes) as client:
        assert await fetch_json(URL, WWW, client=client, resolver=public_resolver) == {"ok": True}
        with pytest.raises(FetchError):
            await fetch_json(URL, NONE, client=client, resolver=public_resolver)

    evil = {URL: httpx.Response(302, headers={"location": "https://assets.brand.example/brand.json"})}
    async with _client(evil) as client:
        with pytest.raises(FetchError):
            await fetch_json(URL, WWW, client=client, resolver=public_resolver)


async def test_same_site_allows_subdomain_but_not_other_site() -> None:
    adagents = "https://pub.example/.well-known/adagents.json"
    cdn = "https://cdn.pub.example/adagents.json"
    routes = {adagents: httpx.Response(302, headers={"location": cdn}), cdn: httpx.Response(200, json={"x": 1})}
    async with _client(routes) as client:
        assert await fetch_json(adagents, SITE, client=client, resolver=public_resolver) == {"x": 1}
    other = {adagents: httpx.Response(302, headers={"location": "https://pub.example.net/a.json"})}
    async with _client(other) as client:
        with pytest.raises(FetchError):
            await fetch_json(adagents, SITE, client=client, resolver=public_resolver)


async def test_refuses_http_private_addresses_and_localhost() -> None:
    async with _client({}) as client:
        for url in ("http://brand.example/x", "https://127.0.0.1/x", "https://[::1]/x", "https://localhost/x"):
            with pytest.raises(FetchError):
                await fetch_json(url, WWW, client=client, resolver=public_resolver)
        with pytest.raises(FetchError):
            await fetch_json(URL, WWW, client=client, resolver=private_resolver)


async def test_body_cap_enforced() -> None:
    big = json.dumps({"k": "x" * 2000})
    async with _client({URL: httpx.Response(200, content=big.encode())}) as client:
        with pytest.raises(FetchError):
            await fetch_json(URL, WWW, client=client, resolver=public_resolver)


def test_strict_json_rejects_duplicates_and_deep_nesting() -> None:
    with pytest.raises(FetchError):
        parse_strict_json(b'{"a": 1, "a": 2}')
    deep = "[" * 40 + "]" * 40
    with pytest.raises(FetchError):
        parse_strict_json(deep.encode())
    with pytest.raises(FetchError):
        parse_strict_json(b"{not json")
