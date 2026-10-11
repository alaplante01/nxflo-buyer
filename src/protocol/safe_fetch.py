"""SSRF-aware JSON fetcher for AdCP trust-root documents (brand.json, adagents.json).

Rules applied to every hop (docs/building/by-layer/L1/security, brand-json, adagents):
- HTTPS only; IP-literal or DNS-resolved private/loopback/link-local/reserved targets
  are refused.
- Redirects are followed manually under a per-document policy (none, exact www
  counterpart, or same-site) with a hop limit; every hop is re-validated.
- The body is capped while streaming (decoded bytes), parsed strictly with duplicate
  object keys rejected, and nesting depth is bounded.

Known limitation: DNS is resolved for validation but the connection is not pinned to
the validated address, so a DNS-rebinding race is not fully closed.
"""

import asyncio
import ipaddress
import json
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from src.protocol.url_canon import MalformedUrlError, canonicalize_url

MAX_BRAND_JSON_BYTES = 2_097_152
MAX_ADAGENTS_BYTES = 4_194_304
MAX_JSON_DEPTH = 32

Resolver = Callable[[str, int], Awaitable[list[str]]]


class RedirectRule(StrEnum):
    NONE = "none"  # explicit URLs (authoritative_location): zero redirects
    WWW_ONLY = "www_only"  # brand.json well-known: host <-> exact www counterpart
    SAME_SITE = "same_site"  # adagents.json well-known: stay within the site


@dataclass(frozen=True)
class FetchPolicy:
    max_bytes: int
    redirect_rule: RedirectRule
    max_redirects: int = 3
    connect_timeout: float = 5.0
    total_timeout: float = 10.0
    max_depth: int = MAX_JSON_DEPTH


class FetchError(Exception):
    """A trust-root document could not be fetched or parsed safely."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code

    @property
    def not_found(self) -> bool:
        return self.status_code == 404


async def default_resolver(host: str, port: int) -> list[str]:
    """Resolve a host to IP address strings using the running loop."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def _is_public_ip(value: str) -> bool:
    address = ipaddress.ip_address(value.split("%", 1)[0])
    return address.is_global and not address.is_multicast


async def assert_public_https_url(url: str, resolver: Resolver) -> None:
    """Refuse non-HTTPS URLs and hosts that are, or resolve to, non-public addresses."""
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise FetchError(f"refusing non-HTTPS URL: {url}")
    host = parts.hostname
    if not host:
        raise FetchError(f"URL has no host: {url}")
    if host == "localhost" or host.endswith(".localhost"):
        raise FetchError(f"refusing local host: {host}")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not _is_public_ip(host):
            raise FetchError(f"refusing non-public address: {host}")
        return
    try:
        addresses = await resolver(host, parts.port or 443)
    except OSError as exc:
        raise FetchError(f"cannot resolve {host}: {exc}") from exc
    if not addresses:
        raise FetchError(f"cannot resolve {host}")
    for address in addresses:
        if not _is_public_ip(address):
            raise FetchError(f"{host} resolves to non-public address {address}")


def _redirect_allowed(rule: RedirectRule, original: str, target: str) -> bool:
    try:
        orig = canonicalize_url(original)
        nxt = canonicalize_url(target)
    except MalformedUrlError:
        return False
    if nxt.scheme != "https" or rule == RedirectRule.NONE:
        return False
    orig_port = orig.authority[len(orig.host) :]
    next_port = nxt.authority[len(nxt.host) :]
    if orig_port != next_port:
        return False
    if nxt.host == orig.host:
        return True
    is_www_pair = nxt.host == "www." + orig.host or orig.host == "www." + nxt.host
    if rule == RedirectRule.WWW_ONLY:
        return is_www_pair
    # SAME_SITE: conservative subset of "same registrable domain" without a public
    # suffix list: the exact www counterpart, or a subdomain of the original host.
    return is_www_pair or nxt.host.endswith("." + orig.host)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise FetchError(f"duplicate JSON key: {key[:32]!r}")
        result[key] = value
    return result


def _depth(value: Any) -> int:
    """Nesting depth of a parsed JSON value (iterative, no recursion limit issues)."""
    deepest = 0
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        node, level = stack.pop()
        if isinstance(node, dict):
            deepest = max(deepest, level)
            stack.extend((child, level + 1) for child in node.values())
        elif isinstance(node, list):
            deepest = max(deepest, level)
            stack.extend((child, level + 1) for child in node)
    return deepest


def parse_strict_json(raw: bytes, max_depth: int = MAX_JSON_DEPTH) -> Any:
    """Parse JSON rejecting duplicate keys and excessive nesting."""
    try:
        parsed = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except (ValueError, RecursionError) as exc:
        raise FetchError(f"malformed JSON: {exc}") from exc
    if _depth(parsed) > max_depth:
        raise FetchError(f"JSON nesting exceeds {max_depth}")
    return parsed


async def _read_capped(response: httpx.Response, max_bytes: int) -> bytes:
    declared = response.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        raise FetchError(f"document exceeds {max_bytes} bytes")
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > max_bytes:
            raise FetchError(f"document exceeds {max_bytes} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


async def _fetch(
    url: str,
    policy: FetchPolicy,
    client: httpx.AsyncClient,
    resolver: Resolver,
) -> Any:
    current = url
    for _hop in range(policy.max_redirects + 1):
        await assert_public_https_url(current, resolver)
        async with client.stream(
            "GET",
            current,
            headers={"Accept": "application/json", "Accept-Encoding": "identity"},
            follow_redirects=False,
        ) as response:
            if response.is_redirect:
                location = response.headers.get("location", "")
                target = urljoin(current, location)
                if not location or not _redirect_allowed(policy.redirect_rule, url, target):
                    raise FetchError(f"redirect from {current} to {target!r} not permitted")
                current = target
                continue
            if response.status_code != 200:
                raise FetchError(
                    f"GET {current} returned HTTP {response.status_code}",
                    status_code=response.status_code,
                )
            body = await _read_capped(response, policy.max_bytes)
        return parse_strict_json(body, policy.max_depth)
    raise FetchError(f"too many redirects fetching {url}")


async def fetch_json(
    url: str,
    policy: FetchPolicy,
    *,
    client: httpx.AsyncClient | None = None,
    resolver: Resolver | None = None,
) -> Any:
    """Fetch and strictly parse a JSON document under ``policy``."""
    resolve = resolver or default_resolver
    timeout = httpx.Timeout(policy.total_timeout, connect=policy.connect_timeout)
    try:
        async with asyncio.timeout(policy.total_timeout):
            if client is not None:
                return await _fetch(url, policy, client, resolve)
            async with httpx.AsyncClient(timeout=timeout) as owned:
                return await _fetch(url, policy, owned, resolve)
    except TimeoutError as exc:
        raise FetchError(f"timed out fetching {url}") from exc
    except httpx.HTTPError as exc:
        raise FetchError(f"error fetching {url}: {exc}") from exc
