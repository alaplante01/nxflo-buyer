"""httpx2 auth hook that signs MCP ``tools/call`` requests (used by fastmcp transports).

Only body-bearing JSON-RPC ``tools/call`` POSTs whose tool is selected by the
caller-supplied predicate are signed; ``initialize``, ``tools/list``, SSE GETs and
session DELETEs pass through unsigned. The bearer ``Authorization`` header (if any)
is left in place: verifiers evaluate a presented signature on its own merits.
"""

import json
import logging
from collections.abc import Callable, Generator
from typing import Any

import httpx2

from src.signing.keys import SigningKey
from src.signing.rfc9421 import sign_request

logger = logging.getLogger(__name__)

ShouldSign = Callable[[str, dict[str, Any]], bool]


def mcp_tool_call(body: bytes) -> tuple[str, dict[str, Any]] | None:
    """(tool name, arguments) when ``body`` is a JSON-RPC tools/call request."""
    if not body:
        return None
    try:
        envelope = json.loads(body)
    except ValueError:
        return None
    if not isinstance(envelope, dict) or envelope.get("method") != "tools/call":
        return None
    params = envelope.get("params")
    if not isinstance(params, dict) or not isinstance(params.get("name"), str):
        return None
    arguments = params.get("arguments")
    return params["name"], arguments if isinstance(arguments, dict) else {}


class AdcpSigningAuth(httpx2.Auth):
    """Adds RFC 9421 Signature / Signature-Input / Content-Digest headers."""

    requires_request_body = True

    def __init__(self, key: SigningKey, should_sign: ShouldSign) -> None:
        self._key = key
        self._should_sign = should_sign

    def auth_flow(
        self, request: httpx2.Request
    ) -> Generator[httpx2.Request, httpx2.Response, None]:
        body = request.content
        call = mcp_tool_call(body) if request.method.upper() == "POST" else None
        if call is not None and self._should_sign(*call):
            signed = sign_request(
                request.method, str(request.url), dict(request.headers), body, self._key
            )
            for name, value in signed.items():
                request.headers[name] = value
            logger.debug("Signed %s request to %s with kid %s", call[0], request.url, self._key.kid)
        yield request
