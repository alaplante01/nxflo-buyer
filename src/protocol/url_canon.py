"""AdCP URL canonicalization (docs/reference/url-canonicalization).

One algorithm governs every place AdCP compares URLs as identifiers: the RFC 9421
``@target-uri`` / ``@authority`` signing components and ``adagents.json``
``authorized_agents[].url`` matching. Steps, in order:

1. lowercase scheme; 2. canonicalize host (UTS-46 nontransitional ToASCII, lowercase,
strip exactly one trailing root dot, IPv6 brackets kept, zone ids rejected);
3. strip userinfo (reject host-less authorities); 4. strip default ports;
5. remove dot segments but keep consecutive slashes; 6. normalize percent-encoding
(uppercase hex, decode unreserved); 7. keep the query byte-for-byte; 8. drop fragment.
"""

import ipaddress
import re
from dataclasses import dataclass

import idna

_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*):")
_PCT_RE = re.compile(r"%([0-9A-Fa-f]{2})")
_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_DEFAULT_PORTS = {"https": "443", "http": "80"}


class MalformedUrlError(ValueError):
    """The URL cannot be canonicalized (maps to request_target_uri_malformed)."""


@dataclass(frozen=True)
class CanonicalUrl:
    """Canonical target URI plus its derived ``@authority``."""

    target_uri: str
    authority: str
    scheme: str
    host: str


def _normalize_percent(segment: str) -> str:
    def repl(match: re.Match[str]) -> str:
        char = chr(int(match.group(1), 16))
        if char in _UNRESERVED:
            return char
        return "%" + match.group(1).upper()

    return _PCT_RE.sub(repl, segment)


def _remove_dot_segments(path: str) -> str:
    """RFC 3986 §5.2.4 remove_dot_segments; consecutive slashes are preserved."""
    output: list[str] = []
    data = path
    while data:
        if data.startswith("../"):
            data = data[3:]
        elif data.startswith("./"):
            data = data[2:]
        elif data.startswith("/./"):
            data = "/" + data[3:]
        elif data == "/.":
            data = "/"
        elif data.startswith("/../"):
            data = "/" + data[4:]
            if output:
                output.pop()
        elif data == "/..":
            data = "/"
            if output:
                output.pop()
        elif data in (".", ".."):
            data = ""
        else:
            start = 1 if data.startswith("/") else 0
            cut = data.find("/", start)
            if cut == -1:
                output.append(data)
                data = ""
            else:
                output.append(data[:cut])
                data = data[cut:]
    return "".join(output)


def _canonical_ipv6(host: str) -> str:
    inner = host[1:-1]
    if "%" in inner:
        raise MalformedUrlError("IPv6 zone identifiers are not allowed")
    try:
        ipaddress.IPv6Address(inner)
    except ValueError as exc:
        raise MalformedUrlError(f"invalid IPv6 literal: {host}") from exc
    return "[" + inner.lower() + "]"


def _canonical_dns_host(host: str) -> str:
    if host.endswith(".."):
        raise MalformedUrlError("host has an empty DNS label")
    trimmed = host.removesuffix(".")
    if not trimmed:
        raise MalformedUrlError("authority missing host")
    if trimmed.isascii():
        ascii_host = trimmed.lower()
        try:
            ipaddress.IPv4Address(ascii_host)
        except ValueError:
            pass
        else:
            return ascii_host
    try:
        encoded = idna.encode(trimmed, uts46=True, transitional=False, std3_rules=True).decode(
            "ascii"
        )
    except idna.IDNAError as exc:
        raise MalformedUrlError(f"invalid host: {host}") from exc
    return encoded.lower()


def _split_authority(rest: str) -> tuple[str, str]:
    """Split ``//authority/path?query`` into (authority, remainder)."""
    end = len(rest)
    for sep in ("/", "?", "#"):
        idx = rest.find(sep)
        if idx != -1:
            end = min(end, idx)
    return rest[:end], rest[end:]


def _parse_host_port(authority: str) -> tuple[str, str]:
    hostport = authority.rsplit("@", 1)[-1]
    if not hostport:
        raise MalformedUrlError("authority missing host")
    if hostport.startswith("["):
        close = hostport.find("]")
        if close == -1:
            raise MalformedUrlError("bracketed host missing closing bracket")
        host = hostport[: close + 1]
        tail = hostport[close + 1 :]
        if tail and not tail.startswith(":"):
            raise MalformedUrlError("unexpected characters after IPv6 literal")
        port = tail[1:] if tail else ""
        return host, port
    if hostport.count(":") > 1:
        raise MalformedUrlError("bare IPv6 address outside brackets")
    host, _, port = hostport.partition(":")
    if not host:
        raise MalformedUrlError("authority missing host")
    return host, port


def canonicalize_url(url: str) -> CanonicalUrl:
    """Canonicalize an absolute http(s) URL per the AdCP algorithm."""
    match = _SCHEME_RE.match(url)
    if not match:
        raise MalformedUrlError("URL has no scheme")
    scheme = match.group(1).lower()
    rest = url[match.end() :]
    if not rest.startswith("//"):
        raise MalformedUrlError("URL has no authority")
    authority, remainder = _split_authority(rest[2:])
    raw_host, port = _parse_host_port(authority)
    if any(ord(ch) > 127 for ch in raw_host) and not raw_host.startswith("["):
        host = _canonical_dns_host(raw_host)
    elif raw_host.startswith("["):
        host = _canonical_ipv6(raw_host)
    else:
        host = _canonical_dns_host(raw_host)
    if port and not port.isdigit():
        raise MalformedUrlError(f"invalid port: {port}")
    if port and _DEFAULT_PORTS.get(scheme) == port:
        port = ""
    canonical_authority = f"{host}:{port}" if port else host

    remainder = remainder.split("#", 1)[0]
    path, sep, query = remainder.partition("?")
    path = _remove_dot_segments(_normalize_percent(path)) if path else ""
    if not path:
        path = "/"
    target = f"{scheme}://{canonical_authority}{path}"
    if sep:
        target += "?" + _normalize_percent(query)
    return CanonicalUrl(target_uri=target, authority=canonical_authority, scheme=scheme, host=host)


def urls_equivalent(left: str, right: str) -> bool:
    """Compare two URLs as identifiers; malformed URLs never match."""
    try:
        return canonicalize_url(left).target_uri == canonicalize_url(right).target_uri
    except MalformedUrlError:
        return False
