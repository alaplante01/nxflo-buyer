"""AdCP RFC 9421 request-signing profile (docs/building/by-layer/L1/security).

Covered components: ``@method``, ``@target-uri`` (AdCP-canonicalized), ``@authority``
and, for body-bearing requests, ``content-type`` and ``content-digest`` (mandatory in
the 3.2 profile). Parameters: created, expires (<= 300 s), nonce (>= 128-bit,
unpadded base64url), keyid, alg, tag = ``adcp/request-signing/v1``. Binary values
(``Signature``, ``Content-Digest``) use RFC 8941 sf-binary with standard padded Base64.
"""

import base64
import hashlib
import os
import time
from collections.abc import Mapping

from src.protocol.url_canon import canonicalize_url
from src.signing.keys import SigningKey, b64url

TAG = "adcp/request-signing/v1"
LABEL = "sig1"
MAX_VALIDITY_SECONDS = 300
COMPONENTS_WITH_BODY = ("@method", "@target-uri", "@authority", "content-type", "content-digest")
COMPONENTS_NO_BODY = ("@method", "@target-uri", "@authority")


def sf_binary(data: bytes) -> str:
    return ":" + base64.b64encode(data).decode("ascii") + ":"


def content_digest(body: bytes) -> str:
    """RFC 9530 Content-Digest using sha-256."""
    return "sha-256=" + sf_binary(hashlib.sha256(body).digest())


def new_nonce() -> str:
    return b64url(os.urandom(16))


def signature_params(
    components: tuple[str, ...], *, created: int, expires: int, nonce: str, keyid: str, alg: str
) -> str:
    covered = " ".join(f'"{c}"' for c in components)
    return (
        f"({covered});created={created};expires={expires};"
        f'nonce="{nonce}";keyid="{keyid}";alg="{alg}";tag="{TAG}"'
    )


def signature_base(
    method: str,
    url: str,
    headers: Mapping[str, str],
    components: tuple[str, ...],
    params: str,
) -> str:
    """RFC 9421 §2.5 signature base for the AdCP component set."""
    canonical = canonicalize_url(url)
    lowered = {k.lower(): v.strip() for k, v in headers.items()}
    lines: list[str] = []
    for component in components:
        if component == "@method":
            value = method.upper()
        elif component == "@target-uri":
            value = canonical.target_uri
        elif component == "@authority":
            value = canonical.authority
        else:
            value = lowered[component]
        lines.append(f'"{component}": {value}')
    lines.append(f'"@signature-params": {params}')
    return "\n".join(lines)


def sign_request(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes,
    key: SigningKey,
    *,
    created: int | None = None,
    nonce: str | None = None,
    validity: int = MAX_VALIDITY_SECONDS,
) -> dict[str, str]:
    """Headers to add: Signature-Input, Signature and (with a body) Content-Digest."""
    issued = int(time.time()) if created is None else created
    expires = issued + min(validity, MAX_VALIDITY_SECONDS)
    out: dict[str, str] = {}
    merged = dict(headers)
    if body:
        if not any(k.lower() == "content-type" for k in merged):
            raise ValueError("body-bearing signed requests must carry Content-Type")
        out["Content-Digest"] = content_digest(body)
        merged["content-digest"] = out["Content-Digest"]
        components = COMPONENTS_WITH_BODY
    else:
        components = COMPONENTS_NO_BODY
    params = signature_params(
        components,
        created=issued,
        expires=expires,
        nonce=nonce or new_nonce(),
        keyid=key.kid,
        alg=key.alg,
    )
    base = signature_base(method, url, merged, components, params)
    out["Signature-Input"] = f"{LABEL}={params}"
    out["Signature"] = f"{LABEL}={sf_binary(key.sign(base.encode('utf-8')))}"
    return out
