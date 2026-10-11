"""Sales-agent authorization against publisher adagents.json (AdCP Property Protocol).

Before ``create_media_buy`` the buyer checks that the seller agent is authorized by
each publisher whose properties the selected product claims
(docs/governance/property/authorized-properties, docs/governance/property/adagents):

- each ``product.publisher_properties[]`` selector names a publisher domain and a
  property selection (``all`` / ``by_id`` / ``by_tag``);
- the publisher's ``/.well-known/adagents.json`` (one ``authoritative_location`` hop
  allowed) must list the seller's agent URL (AdCP URL canonicalization, path included)
  in ``authorized_agents[]`` with an in-window grant covering every claimed property;
- missing/unreachable/invalid files, revoked domains, collection-scoped grants,
  unmet country or placement scopes, and products without ``publisher_properties``
  all fail closed.
"""

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from src.config import settings
from src.protocol.safe_fetch import (
    MAX_ADAGENTS_BYTES,
    FetchError,
    FetchPolicy,
    RedirectRule,
    fetch_json,
)
from src.protocol.url_canon import urls_equivalent

logger = logging.getLogger(__name__)

WELL_KNOWN_POLICY = FetchPolicy(max_bytes=MAX_ADAGENTS_BYTES, redirect_rule=RedirectRule.SAME_SITE)
EXPLICIT_POLICY = FetchPolicy(max_bytes=MAX_ADAGENTS_BYTES, redirect_rule=RedirectRule.NONE)

Fetcher = Callable[[str, FetchPolicy], Awaitable[Any]]


class SellerNotAuthorizedError(Exception):
    """The seller agent is not authorized for the product's publisher properties."""

    def __init__(self, seller_url: str, publisher_domain: str | None, reason: str) -> None:
        where = f" for {publisher_domain}" if publisher_domain else ""
        super().__init__(f"Seller {seller_url} is not authorized{where}: {reason}")
        self.seller_url = seller_url
        self.publisher_domain = publisher_domain
        self.reason = reason


@dataclass
class _CacheEntry:
    expires_at: float
    document: dict[str, Any] | None = None
    error: str | None = None


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _properties(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Valid property objects; malformed entries are skipped, never fatal."""
    valid: list[dict[str, Any]] = []
    for index, prop in enumerate(document.get("properties") or []):
        if isinstance(prop, dict) and prop.get("property_id"):
            valid.append(prop)
        else:
            logger.warning("adagents property #%d skipped: missing property_id", index)
    return valid


def _ids_with_tags(props: list[dict[str, Any]], tags: list[Any]) -> set[str]:
    wanted = {str(t) for t in tags}
    return {str(p["property_id"]) for p in props if wanted & {str(t) for t in p.get("tags") or []}}


def _claimed_ids(selector: dict[str, Any], props: list[dict[str, Any]]) -> set[str]:
    """Property IDs a product selector claims within the publisher's file."""
    selection = selector.get("selection_type")
    if selection == "all":
        return {str(p["property_id"]) for p in props}
    if selection == "by_id":
        return {str(i) for i in selector.get("property_ids") or []}
    if selection == "by_tag":
        return _ids_with_tags(props, selector.get("property_tags") or [])
    return set()


def _granted_ids(entry: dict[str, Any], domain: str, props: list[dict[str, Any]]) -> set[str]:
    """Property IDs one authorized_agents[] entry grants for ``domain``."""
    kind = entry.get("authorization_type")
    if kind == "property_ids":
        return {str(i) for i in entry.get("property_ids") or []}
    if kind == "property_tags":
        return _ids_with_tags(props, entry.get("property_tags") or [])
    if kind == "inline_properties":
        return {
            str(p["property_id"])
            for p in entry.get("properties") or []
            if isinstance(p, dict) and p.get("property_id")
        }
    if kind == "publisher_properties":
        granted: set[str] = set()
        for sel in entry.get("publisher_properties") or []:
            if not isinstance(sel, dict):
                continue
            domains = (
                [sel.get("publisher_domain")]
                if sel.get("publisher_domain")
                else sel.get("publisher_domains") or []
            )
            if domain in {str(d).lower() for d in domains}:
                granted |= _claimed_ids(sel, props)
        return granted
    return set()


def _entry_in_scope(
    entry: dict[str, Any],
    *,
    now: datetime,
    countries: list[str] | None,
    placement_ids: list[str] | None,
) -> str | None:
    """Return a reason the entry does not apply, or None when it applies."""
    start = _parse_time(entry.get("effective_from"))
    end = _parse_time(entry.get("effective_until"))
    if start is not None and now < start:
        return "authorization not yet effective"
    if end is not None and now >= end:
        return "authorization expired"
    if entry.get("collections"):
        return "grant is collection-scoped; product carries no collection scope"
    allowed_countries = entry.get("countries")
    if allowed_countries:
        requested = {c.upper() for c in countries or []}
        if not requested or not requested.issubset({str(c).upper() for c in allowed_countries}):
            return "grant is country-scoped and the buy's countries are not within it"
    allowed_placements = {str(p) for p in entry.get("placement_ids") or []}
    if allowed_placements and not (placement_ids and set(placement_ids) <= allowed_placements):
        return "grant is placement-scoped and the product's placements are not within it"
    if entry.get("placement_tags") and not allowed_placements:
        return "grant is placement-tag-scoped; placement tags are not resolved by this buyer"
    return None


def _product_placement_ids(product: dict[str, Any]) -> list[str] | None:
    placements = product.get("placements")
    if not isinstance(placements, list):
        return None
    ids = [
        str(p.get("placement_id"))
        for p in placements
        if isinstance(p, dict) and p.get("placement_id")
    ]
    return ids or None


class SalesAgentAuthorizer:
    """Fetches/caches adagents.json and validates a seller for a product."""

    def __init__(
        self,
        fetcher: Fetcher | None = None,
        *,
        ttl_seconds: int | None = None,
        negative_ttl_seconds: int | None = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._fetch: Fetcher = fetcher or (lambda url, policy: fetch_json(url, policy))
        self._ttl = settings.adagents_cache_ttl_seconds if ttl_seconds is None else ttl_seconds
        self._negative_ttl = (
            settings.adagents_negative_cache_ttl_seconds
            if negative_ttl_seconds is None
            else negative_ttl_seconds
        )
        self._clock = clock
        self._now = now
        self._cache: dict[str, _CacheEntry] = {}

    async def _load(self, domain: str) -> dict[str, Any]:
        document = await self._fetch(
            f"https://{domain}/.well-known/adagents.json", WELL_KNOWN_POLICY
        )
        if not isinstance(document, dict):
            raise FetchError("adagents.json is not a JSON object")
        location = document.get("authoritative_location")
        if isinstance(location, str):
            if not location.startswith("https://"):
                raise FetchError("authoritative_location must be HTTPS")
            document = await self._fetch(location, EXPLICIT_POLICY)
            if not isinstance(document, dict) or "authoritative_location" in document:
                raise FetchError(
                    "authoritative file is not an inline adagents.json (nested reference)"
                )
        if not isinstance(document.get("authorized_agents"), list):
            raise FetchError("adagents.json has no authorized_agents array")
        return document

    async def get_adagents(self, domain: str) -> dict[str, Any]:
        """Return a publisher's adagents.json (cached); raises FetchError on failure."""
        key = domain.lower()
        entry = self._cache.get(key)
        if entry is not None and entry.expires_at > self._clock():
            if entry.document is not None:
                return entry.document
            raise FetchError(entry.error or "adagents.json unavailable")
        try:
            document = await self._load(key)
        except FetchError as exc:
            self._cache[key] = _CacheEntry(
                expires_at=self._clock() + self._negative_ttl, error=str(exc)
            )
            raise
        self._cache[key] = _CacheEntry(expires_at=self._clock() + self._ttl, document=document)
        return document

    async def verify_product(
        self,
        seller_url: str,
        product: dict[str, Any],
        *,
        countries: list[str] | None = None,
    ) -> None:
        """Raise SellerNotAuthorizedError unless every claimed property is authorized."""
        selectors = product.get("publisher_properties")
        if not isinstance(selectors, list) or not selectors:
            raise SellerNotAuthorizedError(
                seller_url,
                None,
                "product declares no publisher_properties; authorization cannot be verified",
            )
        placement_ids = _product_placement_ids(product)
        for selector in selectors:
            await self._verify_selector(
                seller_url, selector, countries=countries, placement_ids=placement_ids
            )

    async def _verify_selector(
        self,
        seller_url: str,
        selector: object,
        *,
        countries: list[str] | None,
        placement_ids: list[str] | None,
    ) -> None:
        if not isinstance(selector, dict) or not isinstance(selector.get("publisher_domain"), str):
            raise SellerNotAuthorizedError(
                seller_url,
                None,
                "publisher_properties entry must use the singular publisher_domain form",
            )
        domain = selector["publisher_domain"].lower()
        try:
            document = await self.get_adagents(domain)
        except FetchError as exc:
            raise SellerNotAuthorizedError(
                seller_url, domain, f"adagents.json unavailable ({exc})"
            ) from exc
        revoked = {
            str(r.get("publisher_domain", "")).lower()
            for r in document.get("revoked_publisher_domains") or []
            if isinstance(r, dict)
        }
        if domain in revoked:
            raise SellerNotAuthorizedError(
                seller_url, domain, "publisher domain revoked in adagents.json"
            )
        props = _properties(document)
        claimed = _claimed_ids(selector, props)
        if not claimed:
            raise SellerNotAuthorizedError(
                seller_url, domain, "product selector resolves to no publisher properties"
            )
        known = {str(p["property_id"]) for p in props}
        unknown = claimed - known
        if unknown and selector.get("selection_type") == "by_id":
            raise SellerNotAuthorizedError(
                seller_url, domain, f"unknown property ids {sorted(unknown)}"
            )
        entries = [
            e
            for e in document["authorized_agents"]
            if isinstance(e, dict)
            and isinstance(e.get("url"), str)
            and urls_equivalent(e["url"], seller_url)
        ]
        if not entries:
            raise SellerNotAuthorizedError(
                seller_url, domain, "agent URL not listed in authorized_agents"
            )
        granted: set[str] = set()
        reasons: list[str] = []
        now = self._now()
        for entry in entries:
            reason = _entry_in_scope(
                entry, now=now, countries=countries, placement_ids=placement_ids
            )
            if reason:
                reasons.append(reason)
                continue
            granted |= _granted_ids(entry, domain, props)
        missing = claimed - granted
        if missing:
            detail = "; ".join(reasons) if reasons else f"properties not granted: {sorted(missing)}"
            raise SellerNotAuthorizedError(seller_url, domain, detail)
