"""brand.json resolution and brand-operator authorization (AdCP Brand Protocol).

Before buying on a brand's behalf the buyer resolves the advertiser's
``/.well-known/brand.json`` and checks that our operator domain is listed in the
house's ``authorized_operators[]`` for the brand (docs/brand-protocol/brand-json,
docs/building/by-layer/L2/accounts-and-agents#authorized-operators). The resolver is
named after the file it consults, per the spec's SDK naming guidance.

Fail-closed rules implemented here:
- operator == brand domain is implicit self-authorization (no listing needed);
- standalone Brand Canonical Documents cannot authorize third-party operators;
- a leaf whose ``house_domain`` is not reciprocated by the house's ``brand_refs[]``
  is unverified at the relationship layer, so the house's operators do not apply;
- Brand Agent documents (identity behind an MCP agent) are not resolved statically.
"""

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from src.config import settings
from src.protocol.safe_fetch import (
    MAX_BRAND_JSON_BYTES,
    FetchError,
    FetchPolicy,
    RedirectRule,
    fetch_json,
)

logger = logging.getLogger(__name__)

MAX_DOCUMENT_HOPS = 3
WELL_KNOWN_POLICY = FetchPolicy(max_bytes=MAX_BRAND_JSON_BYTES, redirect_rule=RedirectRule.WWW_ONLY)
EXPLICIT_POLICY = FetchPolicy(max_bytes=MAX_BRAND_JSON_BYTES, redirect_rule=RedirectRule.NONE)

Fetcher = Callable[[str, FetchPolicy], Awaitable[Any]]


class BrandAuthorizationError(Exception):
    """Brand identity could not be resolved or our operator is not authorized."""

    def __init__(self, code: str, brand_domain: str, message: str) -> None:
        super().__init__(f"[{code}] {brand_domain}: {message}")
        self.code = code
        self.brand_domain = brand_domain


@dataclass(frozen=True)
class BrandResolution:
    """Outcome of resolving a brand domain to its canonical identity."""

    domain: str
    kind: str  # house_portfolio | brand_canonical | self
    brand_id: str | None
    house_domain: str | None
    authorized_operators: list[dict[str, Any]] = field(default_factory=list)
    relationship_verified: bool = False
    brand_ids: list[str] = field(default_factory=list)


@dataclass
class _CacheEntry:
    expires_at: float
    resolution: BrandResolution | None = None
    error: BrandAuthorizationError | None = None


def normalize_domain(value: str) -> str:
    """Lowercase a domain and strip one trailing root dot."""
    return value.strip().lower().removesuffix(".")


def domain_from_url(url: str) -> str:
    """Derive a brand domain from a URL (leading 'www.' is dropped)."""
    host = urlsplit(url if "://" in url else f"https://{url}").hostname or ""
    return normalize_domain(host).removeprefix("www.")


def build_brand_ref(
    domain: str, brand_id: str | None = None, countries: list[str] | None = None
) -> dict[str, Any]:
    """BrandRef (BrandKey fields only): domain, optional brand_id, sorted countries."""
    ref: dict[str, Any] = {"domain": normalize_domain(domain)}
    if brand_id:
        ref["brand_id"] = brand_id
    if countries:
        ref["countries"] = sorted({c.strip().upper() for c in countries if c.strip()})
    return ref


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def operator_entry_authorizes(
    entry: dict[str, Any],
    operator_domain: str,
    brand_id: str | None,
    *,
    scope: str,
    countries: list[str] | None,
    now: datetime,
) -> bool:
    """Whether one authorized_operators[] entry covers this operator/brand/scope."""
    if normalize_domain(str(entry.get("domain", ""))) != normalize_domain(operator_domain):
        return False
    brands = entry.get("brands")
    if not isinstance(brands, list):
        return False
    if "*" not in brands and (brand_id is None or brand_id not in brands):
        return False
    valid_from = _parse_time(entry.get("valid_from"))
    valid_until = _parse_time(entry.get("valid_until"))
    if valid_from is not None and now < valid_from:
        return False
    if valid_until is not None and now >= valid_until:
        return False
    scopes = entry.get("scopes")
    if isinstance(scopes, list) and "all" not in scopes and scope not in scopes:
        return False
    allowed = entry.get("countries")
    if isinstance(allowed, list) and allowed:
        requested = {c.upper() for c in countries or []}
        if not requested or not requested.issubset({str(c).upper() for c in allowed}):
            return False
    return True


def _is_house_portfolio(doc: dict[str, Any]) -> bool:
    return isinstance(doc.get("house"), dict) and ("brands" in doc or "brand_refs" in doc)


def _is_brand_canonical(doc: dict[str, Any]) -> bool:
    return "id" in doc and "names" in doc and not isinstance(doc.get("house"), dict)


def _property_domains(brand: dict[str, Any]) -> set[str]:
    domains: set[str] = set()
    for prop in brand.get("properties") or []:
        if isinstance(prop, dict) and prop.get("type") == "website" and prop.get("identifier"):
            domains.add(normalize_domain(str(prop["identifier"])).removeprefix("www."))
    return domains


class BrandAuthorizationResolver:
    """Resolves brand.json with caching and checks authorized_operators."""

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
        self._ttl = settings.brand_cache_ttl_seconds if ttl_seconds is None else ttl_seconds
        self._negative_ttl = (
            settings.brand_negative_cache_ttl_seconds
            if negative_ttl_seconds is None
            else negative_ttl_seconds
        )
        self._clock = clock
        self._now = now
        self._cache: dict[tuple[str, str | None], _CacheEntry] = {}

    async def _get(self, url: str, policy: FetchPolicy, brand_domain: str) -> dict[str, Any]:
        try:
            doc = await self._fetch(url, policy)
        except FetchError as exc:
            raise BrandAuthorizationError("brand_json_unavailable", brand_domain, str(exc)) from exc
        if not isinstance(doc, dict):
            raise BrandAuthorizationError(
                "brand_json_malformed", brand_domain, f"{url} is not an object"
            )
        return doc

    async def _follow(self, domain: str, brand_domain: str) -> tuple[dict[str, Any], str]:
        """Fetch a domain's brand.json and follow redirect variants (<= 3 document hops)."""
        current_domain = domain
        doc = await self._get(
            f"https://{domain}/.well-known/brand.json", WELL_KNOWN_POLICY, brand_domain
        )
        for _ in range(MAX_DOCUMENT_HOPS + 1):
            location = doc.get("authoritative_location")
            house = doc.get("house")
            if isinstance(location, str):
                if not location.startswith("https://"):
                    raise BrandAuthorizationError(
                        "brand_json_malformed", brand_domain, "non-HTTPS authoritative_location"
                    )
                doc = await self._get(location, EXPLICIT_POLICY, brand_domain)
            elif isinstance(house, str):
                current_domain = normalize_domain(house)
                doc = await self._get(
                    f"https://{current_domain}/.well-known/brand.json",
                    WELL_KNOWN_POLICY,
                    brand_domain,
                )
            else:
                return doc, current_domain
        raise BrandAuthorizationError(
            "brand_json_redirect_loop", brand_domain, "more than 3 document hops"
        )

    async def _resolve_portfolio_brand(
        self, doc: dict[str, Any], query: str, brand_id: str | None
    ) -> tuple[str | None, bool]:
        """Pick the brand inside a House Portfolio; returns (brand_id, relationship_verified)."""
        house_domain = normalize_domain(str(doc["house"].get("domain", "")))
        inline = [b for b in doc.get("brands") or [] if isinstance(b, dict) and b.get("id")]
        refs = [r for r in doc.get("brand_refs") or [] if isinstance(r, dict) and r.get("brand_id")]
        if brand_id:
            if any(b["id"] == brand_id for b in inline):
                return brand_id, True
            ref = next((r for r in refs if r["brand_id"] == brand_id), None)
            if ref is None:
                raise BrandAuthorizationError(
                    "brand_not_in_portfolio", query, f"brand_id {brand_id!r} not in {house_domain}"
                )
            return brand_id, await self._leaf_reciprocates(
                normalize_domain(str(ref.get("domain", ""))), house_domain, query
            )
        inline_hits = [b["id"] for b in inline if query in _property_domains(b)]
        ref_hits = [r for r in refs if normalize_domain(str(r.get("domain", ""))) == query]
        if len(inline_hits) + len(ref_hits) > 1:
            raise BrandAuthorizationError(
                "brand_ambiguous", query, "several portfolio brands match; pass brand_id"
            )
        if inline_hits:
            return inline_hits[0], True
        if ref_hits:
            return ref_hits[0]["brand_id"], await self._leaf_reciprocates(
                query, house_domain, query
            )
        if len(inline) + len(refs) == 1 and query == house_domain:
            if inline:
                return inline[0]["id"], True
            return refs[0]["brand_id"], await self._leaf_reciprocates(
                normalize_domain(str(refs[0].get("domain", ""))), house_domain, query
            )
        return None, query == house_domain

    async def _leaf_reciprocates(self, leaf_domain: str, house_domain: str, query: str) -> bool:
        """Mutual assertion: the leaf's canonical document names this house."""
        try:
            leaf = await self._get(
                f"https://{leaf_domain}/.well-known/brand.json", WELL_KNOWN_POLICY, query
            )
        except BrandAuthorizationError:
            logger.warning(
                "brand_refs leaf %s unreachable; relationship unverified",
                leaf_domain,
                exc_info=True,
            )
            return False
        return normalize_domain(str(leaf.get("house_domain", ""))) == house_domain

    async def _resolve_uncached(self, domain: str, brand_id: str | None) -> BrandResolution:
        doc, doc_domain = await self._follow(domain, domain)
        if _is_house_portfolio(doc):
            resolved_id, verified = await self._resolve_portfolio_brand(doc, domain, brand_id)
            return BrandResolution(
                domain=domain,
                kind="house_portfolio",
                brand_id=resolved_id,
                house_domain=normalize_domain(str(doc["house"].get("domain", doc_domain))),
                authorized_operators=[
                    o for o in doc.get("authorized_operators") or [] if isinstance(o, dict)
                ],
                relationship_verified=verified,
                brand_ids=[
                    str(b.get("id")) for b in doc.get("brands") or [] if isinstance(b, dict)
                ],
            )
        if _is_brand_canonical(doc):
            return await self._resolve_canonical(doc, domain, brand_id)
        if doc.get("agents") or doc.get("brand_agent"):
            raise BrandAuthorizationError(
                "brand_agent_only",
                domain,
                "brand.json delegates identity to a brand agent; static authorized_operators unavailable",
            )
        raise BrandAuthorizationError(
            "brand_json_malformed", domain, "unrecognised brand.json variant"
        )

    async def _resolve_canonical(
        self, doc: dict[str, Any], domain: str, brand_id: str | None
    ) -> BrandResolution:
        leaf_id = str(doc["id"])
        if brand_id and brand_id != leaf_id:
            raise BrandAuthorizationError(
                "brand_not_in_portfolio", domain, f"brand_id {brand_id!r} != {leaf_id!r}"
            )
        house_domain_raw = doc.get("house_domain")
        if not isinstance(house_domain_raw, str):
            return BrandResolution(
                domain=domain, kind="brand_canonical", brand_id=leaf_id, house_domain=None
            )
        house_doc, house_domain = await self._follow(normalize_domain(house_domain_raw), domain)
        if not _is_house_portfolio(house_doc):
            raise BrandAuthorizationError(
                "house_not_portfolio", domain, f"house {house_domain} is not a House Portfolio"
            )
        house_domain = normalize_domain(str(house_doc["house"].get("domain", house_domain)))
        verified = any(
            isinstance(r, dict)
            and normalize_domain(str(r.get("domain", ""))) == domain
            and r.get("brand_id") == leaf_id
            for r in house_doc.get("brand_refs") or []
        )
        return BrandResolution(
            domain=domain,
            kind="brand_canonical",
            brand_id=leaf_id,
            house_domain=house_domain,
            authorized_operators=[
                o for o in house_doc.get("authorized_operators") or [] if isinstance(o, dict)
            ],
            relationship_verified=verified,
        )

    async def resolve(
        self, domain: str, brand_id: str | None = None, *, force_refresh: bool = False
    ) -> BrandResolution:
        """Resolve a brand domain (cached: positive TTL 24h, failures 1h by default)."""
        key = (normalize_domain(domain), brand_id)
        entry = self._cache.get(key)
        if entry is not None and not force_refresh and entry.expires_at > self._clock():
            if entry.error is not None:
                raise entry.error
            if entry.resolution is not None:
                return entry.resolution
        try:
            resolution = await self._resolve_uncached(key[0], brand_id)
        except BrandAuthorizationError as exc:
            if exc.code in (
                "brand_json_unavailable",
                "brand_json_malformed",
                "brand_json_redirect_loop",
            ):
                self._cache[key] = _CacheEntry(
                    expires_at=self._clock() + self._negative_ttl, error=exc
                )
            raise
        self._cache[key] = _CacheEntry(expires_at=self._clock() + self._ttl, resolution=resolution)
        return resolution

    async def verify_operator(
        self,
        brand: dict[str, Any],
        operator_domain: str,
        *,
        scope: str = "media_buying",
        countries: list[str] | None = None,
        force_refresh: bool = False,
    ) -> BrandResolution:
        """Fail closed unless ``operator_domain`` may act for ``brand`` (a BrandRef)."""
        domain = normalize_domain(str(brand.get("domain", "")))
        if not domain:
            raise BrandAuthorizationError("brand_required", "", "brand.domain is required")
        brand_id = brand.get("brand_id")
        if normalize_domain(operator_domain) == domain:
            return BrandResolution(
                domain=domain,
                kind="self",
                brand_id=brand_id,
                house_domain=None,
                relationship_verified=True,
            )
        resolution = await self.resolve(domain, brand_id, force_refresh=force_refresh)
        if not resolution.authorized_operators:
            raise BrandAuthorizationError(
                "operator_not_authorized",
                domain,
                f"brand.json declares no authorized_operators; {operator_domain} may not buy for this brand",
            )
        if not resolution.relationship_verified:
            raise BrandAuthorizationError(
                "relationship_unverified",
                domain,
                f"brand/house relationship for {domain} is not mutually asserted; house operators do not apply",
            )
        wanted = countries if countries is not None else brand.get("countries")
        now = self._now()
        for entry in resolution.authorized_operators:
            if operator_entry_authorizes(
                entry, operator_domain, resolution.brand_id, scope=scope, countries=wanted, now=now
            ):
                return resolution
        raise BrandAuthorizationError(
            "operator_not_authorized",
            domain,
            f"{operator_domain} is not in authorized_operators for brand {resolution.brand_id or '*'} (scope {scope})",
        )
