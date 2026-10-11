"""Buyer-side MCP endpoint answering ``get_adcp_capabilities``.

A buyer that signs requests must answer ``get_adcp_capabilities`` at its own agent
URL (docs/building/by-layer/L1/request-signing, "Capabilities pointer"): verifiers
start from the agent URL, read ``identity.brand_json_url``, find our ``agents[]``
entry in that brand.json, and fetch our keys from its ``jwks_uri``. Verifiers read
only ``identity``; the schema still requires ``adcp`` and a non-empty
``supported_protocols`` (there is no buyer-only value yet, adcp#6105), so the value
is configurable via NXFLO_AGENT_SUPPORTED_PROTOCOLS.

Mounted by src.main at ``/mcp`` (stateless streamable HTTP, JSON responses); the
public URL of that endpoint is NXFLO_AGENT_URL.
"""

from typing import Any
from urllib.parse import urlsplit

from fastmcp import FastMCP

from src.config import settings

AGENT_ID = "nxflo_buyer"


def build_capabilities(context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Minimal get-adcp-capabilities-response (schemas/3.2.1) for a buying agent."""
    major = int(settings.adcp_version.split(".", 1)[0])
    response: dict[str, Any] = {
        "status": "completed",
        "adcp_version": settings.adcp_version,
        "adcp": {
            "major_versions": [major],
            "supported_versions": [settings.adcp_version],
            # This agent serves no mutating tools, so it makes no replay promise.
            "idempotency": {"supported": False},
        },
        "supported_protocols": list(settings.agent_supported_protocols),
        "identity": {"brand_json_url": settings.resolved_brand_json_url()},
    }
    if context is not None:
        response["context"] = context
    return response


def jwks_uri() -> str:
    """Where src.main serves our JWKS: /.well-known/jwks.json at the agent URL's origin."""
    parts = urlsplit(settings.agent_url)
    return f"{parts.scheme}://{parts.netloc}/.well-known/jwks.json"


def build_brand_json_agent_entry() -> dict[str, Any]:
    """The ``agents[]`` entry our operator's brand.json must publish for this buyer."""
    return {
        "type": "buying",
        "id": AGENT_ID,
        "url": settings.agent_url,
        "jwks_uri": jwks_uri(),
        "description": "nxflo AdCP buying agent",
    }


def create_agent_mcp() -> FastMCP:
    """FastMCP server exposing only get_adcp_capabilities."""
    mcp = FastMCP("nxflo-buyer")

    @mcp.tool(
        name="get_adcp_capabilities",
        description="Discover the AdCP protocols and identity of this buying agent.",
    )
    async def get_adcp_capabilities(
        protocols: list[str] | None = None,
        context: dict[str, Any] | None = None,
        ext: dict[str, Any] | None = None,
        adcp_version: str | None = None,
        adcp_major_version: int | None = None,
    ) -> dict[str, Any]:
        """Protocol filters, version pins and ext are accepted and ignored (buyer agent)."""
        del protocols, ext, adcp_version, adcp_major_version
        return build_capabilities(context)

    return mcp
