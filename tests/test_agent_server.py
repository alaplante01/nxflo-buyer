"""Buyer-side MCP endpoint: get_adcp_capabilities over in-memory and mounted HTTP transports."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastmcp import Client

from src.agent_server.mcp import build_brand_json_agent_entry, build_capabilities, create_agent_mcp
from src.config import settings


def _assert_capabilities(caps: dict[str, Any]) -> None:
    assert caps["status"] == "completed"
    assert caps["adcp"]["major_versions"] == [3]
    assert caps["adcp"]["supported_versions"] == ["3.2"]
    assert caps["adcp"]["idempotency"] == {"supported": False}
    assert caps["supported_protocols"] and set(caps["supported_protocols"]) <= {
        "media_buy", "signals", "governance", "sponsored_intelligence", "creative", "brand", "measurement",
    }
    assert caps["identity"]["brand_json_url"] == "https://nxflo.io/.well-known/brand.json"


def test_capabilities_document() -> None:
    _assert_capabilities(build_capabilities())
    assert build_capabilities({"trace": "t1"})["context"] == {"trace": "t1"}


def test_brand_json_agent_entry() -> None:
    entry = build_brand_json_agent_entry()
    assert entry["type"] == "buying" and entry["url"] == settings.agent_url
    assert entry["jwks_uri"] == "https://buyer.nxflo.io/.well-known/jwks.json"
    assert entry["id"] == "nxflo_buyer"


async def test_in_memory_mcp_call() -> None:
    async with Client(create_agent_mcp()) as client:
        tools = await client.list_tools()
        assert [t.name for t in tools] == ["get_adcp_capabilities"]
        result = await client.call_tool("get_adcp_capabilities", {"protocols": ["media_buy"], "context": {"k": "v"}})
    _assert_capabilities(result.structured_content)
    assert result.structured_content["context"] == {"k": "v"}


def test_mounted_http_endpoint_answers_json_rpc() -> None:
    mcp_app = create_agent_mcp().http_app(path="/mcp", stateless_http=True, json_response=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with mcp_app.lifespan(app):
            yield

    app = FastAPI(lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    app.mount("/", mcp_app)
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        init = client.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "verifier", "version": "1"}},
        })
        assert init.status_code == 200, init.text
        call = client.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "get_adcp_capabilities", "arguments": {}},
        })
        assert call.status_code == 200, call.text
        _assert_capabilities(call.json()["result"]["structuredContent"])
