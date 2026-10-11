"""RFC 9421 request signing (AdCP 3.2 profile), URL canonicalization, keys, JWKS."""

import base64
import json
import os
import re
import stat
from pathlib import Path
from typing import Any

import httpx2
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from fastapi.testclient import TestClient

from src.config import settings
from src.connections.seller import should_sign
from src.discovery.registry import SellerAgent
from src.protocol.url_canon import MalformedUrlError, canonicalize_url, urls_equivalent
from src.signing import keygen
from src.signing.auth import AdcpSigningAuth, mcp_tool_call
from src.signing.keys import (
    SigningKey,
    SigningKeyError,
    b64url_decode,
    build_jwks,
    generate_signing_key,
    load_signing_key,
)
from src.signing.rfc9421 import TAG, content_digest, sign_request, signature_base

FIXTURES = Path(__file__).parent / "fixtures" / "request_signing"
VECTOR_FILES = sorted(p for p in FIXTURES.glob("0*.json"))
PUBLIC_KEYS = {k["kid"]: k for k in json.loads((FIXTURES / "public-keys.json").read_text())["keys"]}


def _verify_with_jwk(jwk: dict, base: bytes, signature: bytes) -> None:
    if jwk["kty"] == "OKP":
        Ed25519PublicKey.from_public_bytes(b64url_decode(jwk["x"])).verify(signature, base)
        return
    numbers = ec.EllipticCurvePublicNumbers(
        int.from_bytes(b64url_decode(jwk["x"]), "big"),
        int.from_bytes(b64url_decode(jwk["y"]), "big"),
        ec.SECP256R1(),
    )
    der = encode_dss_signature(int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:], "big"))
    numbers.public_key().verify(der, base, ec.ECDSA(hashes.SHA256()))


def _sig_bytes(header: str) -> bytes:
    return base64.b64decode(header.split("=", 1)[1].strip(":"))


def _parse_components(signature_input: str) -> tuple[tuple[str, ...], str]:
    params = signature_input.split("sig1=", 1)[1].split(", sig2=")[0]
    inner = re.match(r"\(([^)]*)\)", params)
    assert inner is not None
    return tuple(c.strip('"') for c in inner.group(1).split()), params


# --- URL canonicalization (conformance vectors) ---


@pytest.mark.parametrize(
    "case",
    json.loads((FIXTURES / "canonicalization.json").read_text())["cases"],
    ids=lambda c: c["name"],
)
def test_canonicalization_vectors(case: dict) -> None:
    if case.get("reject"):
        with pytest.raises(MalformedUrlError):
            canonicalize_url(case["input_url"])
        return
    result = canonicalize_url(case["input_url"])
    assert result.target_uri == case["expected_target_uri"]
    if "expected_authority" in case:
        assert result.authority == case["expected_authority"]


def test_urls_equivalent_is_canonical_not_bytewise() -> None:
    assert urls_equivalent("HTTPS://Seller.Example.com:443/mcp", "https://seller.example.com/mcp")
    assert not urls_equivalent("https://seller.example.com", "https://seller.example.com/mcp")
    assert not urls_equivalent("https://seller.example.com/mcp/", "https://seller.example.com/mcp")
    assert not urls_equivalent("not a url", "https://seller.example.com/mcp")


# --- Signature base / profile (3.2 conformance vectors) ---


@pytest.mark.parametrize("path", VECTOR_FILES, ids=lambda p: p.stem)
def test_signature_base_matches_vectors_and_vector_signature_verifies(path: Path) -> None:
    vector = json.loads(path.read_text())
    request = vector["request"]
    headers = request["headers"]
    components, params = _parse_components(headers["Signature-Input"])
    base = signature_base(request["method"], request["url"], headers, components, params)
    assert base == vector["expected_signature_base"]
    if "Content-Digest" in headers and len(headers["Content-Digest"].split(",")) == 1:
        assert content_digest(request["body"].encode()) == headers["Content-Digest"]
    jwk = PUBLIC_KEYS[vector["jwks_ref"][0]]
    _verify_with_jwk(jwk, base.encode(), _sig_bytes(headers["Signature"].split(",")[0]))


@pytest.mark.parametrize("alg", ["ed25519", "ecdsa-p256-sha256"])
def test_sign_request_round_trip(alg: str) -> None:
    key = generate_signing_key(alg, f"test-{alg}")
    body = b'{"jsonrpc":"2.0","method":"tools/call","params":{"name":"create_media_buy"}}'
    url = "https://Seller.Example.com:443/mcp"
    headers = {"Content-Type": "application/json"}
    signed = sign_request("post", url, headers, body, key, created=1_800_000_000, nonce="AAAAAAAAAAAAAAAAAAAAAA")

    assert signed["Content-Digest"] == content_digest(body)
    assert signed["Content-Digest"].startswith("sha-256=:") and signed["Content-Digest"].endswith("=:")
    sig_input = signed["Signature-Input"]
    assert sig_input.startswith('sig1=("@method" "@target-uri" "@authority" "content-type" "content-digest")')
    assert f'tag="{TAG}"' in sig_input and f'alg="{alg}"' in sig_input and f'keyid="{key.kid}"' in sig_input
    assert "created=1800000000;expires=1800000300" in sig_input
    assert re.fullmatch(r"sig1=:[A-Za-z0-9+/]+=*:", signed["Signature"])

    components, params = _parse_components(sig_input)
    merged = {**headers, "content-digest": signed["Content-Digest"]}
    base = signature_base("POST", url, merged, components, params)
    assert '"@target-uri": https://seller.example.com/mcp' in base
    assert '"@authority": seller.example.com' in base
    _verify_with_jwk(key.public_jwk(), base.encode(), _sig_bytes(signed["Signature"]))


def test_nonce_is_128_bit_unpadded_base64url() -> None:
    key = generate_signing_key("ed25519", "k")
    signed = sign_request("POST", "https://s.example/mcp", {"content-type": "application/json"}, b"{}", key)
    nonce = re.search(r'nonce="([^"]+)"', signed["Signature-Input"]).group(1)
    assert "=" not in nonce and len(b64url_decode(nonce)) >= 16


def test_body_requires_content_type() -> None:
    key = generate_signing_key("ed25519", "k")
    with pytest.raises(ValueError):
        sign_request("POST", "https://s.example/mcp", {}, b"{}", key)


# --- Keys and JWKS ---


def test_public_jwk_is_request_signing_and_has_no_private_part() -> None:
    for alg, jwk_alg in (("ed25519", "EdDSA"), ("ecdsa-p256-sha256", "ES256")):
        jwk = generate_signing_key(alg, "kid-1").public_jwk()
        assert jwk["adcp_use"] == "request-signing"
        assert jwk["use"] == "sig" and jwk["key_ops"] == ["verify"] and jwk["alg"] == jwk_alg
        assert "d" not in jwk


def test_load_signing_key_from_pem_and_jwk() -> None:
    original = generate_signing_key("ed25519", "pem-kid")
    loaded = load_signing_key(original.private_pem().decode(), "pem-kid")
    assert loaded.public_jwk() == original.public_jwk()
    with pytest.raises(SigningKeyError):
        load_signing_key(original.private_pem().decode(), None)

    private_raw = original.private_key.private_bytes_raw()
    jwk = {**original.public_jwk(), "d": base64.urlsafe_b64encode(private_raw).rstrip(b"=").decode()}
    from_jwk = load_signing_key(json.dumps(jwk))
    assert from_jwk.kid == "pem-kid" and from_jwk.public_jwk() == original.public_jwk()
    with pytest.raises(SigningKeyError):
        load_signing_key(json.dumps(original.public_jwk()))


def test_build_jwks_includes_rotation_keys_without_private_material(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    active = generate_signing_key("ed25519", "active")
    old = generate_signing_key("ed25519", "old").public_jwk()
    leaked = {**generate_signing_key("ed25519", "leaky").public_jwk(), "d": "secret"}
    extra = tmp_path / "jwks.json"
    extra.write_text(json.dumps({"keys": [old, leaked, active.public_jwk()]}))
    monkeypatch.setattr(settings, "signing_additional_jwks_path", str(extra))
    kids = [k["kid"] for k in build_jwks(active)["keys"]]
    assert kids == ["active", "old"]


def test_keygen_writes_0600_and_never_overwrites(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    private = tmp_path / "key.pem"
    assert keygen.main(["--kid", "nxflo-test", "--private-out", str(private)]) == 0
    assert stat.S_IMODE(os.stat(private).st_mode) == 0o600
    jwks = json.loads(capsys.readouterr().out)
    assert jwks["keys"][0]["kid"] == "nxflo-test" and "d" not in jwks["keys"][0]
    assert load_signing_key(private.read_text(), "nxflo-test").kid == "nxflo-test"
    with pytest.raises(FileExistsError):
        keygen.main(["--kid", "again", "--private-out", str(private)])


def test_jwks_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.main import app

    client = TestClient(app)
    monkeypatch.setattr(settings, "signing_private_key", "")
    monkeypatch.setattr(settings, "signing_private_key_path", "")
    assert client.get("/.well-known/jwks.json").json() == {"keys": []}

    key = generate_signing_key("ed25519", "published-kid")
    monkeypatch.setattr(settings, "signing_private_key", key.private_pem().decode())
    monkeypatch.setattr(settings, "signing_key_id", "published-kid")
    response = client.get("/.well-known/jwks.json")
    assert response.headers["cache-control"].startswith("public")
    assert response.json()["keys"] == [key.public_jwk()]


# --- Outbound hook (httpx2 auth used by the fastmcp transport) ---


def _seller(signing: dict, versions: list[str] | None = None) -> SellerAgent:
    return SellerAgent(
        name="S",
        url="https://seller.example/mcp",
        capabilities={"adcp": {"supported_versions": versions or ["3.2"]}, "request_signing": signing},
    )


def test_should_sign_follows_seller_capabilities() -> None:
    supported = {"supported": True, "covers_content_digest": "required", "supported_for": ["create_media_buy"]}
    assert should_sign(_seller(supported), "create_media_buy", {})
    assert not should_sign(_seller(supported), "get_products", {})
    assert not should_sign(_seller({**supported, "supported": False}), "create_media_buy", {})
    assert not should_sign(_seller({**supported, "covers_content_digest": "forbidden"}), "create_media_buy", {})
    assert not should_sign(_seller(supported, ["3.0", "3.1"]), "create_media_buy", {})
    webhook_args = {"push_notification_config": {"url": "https://b", "authentication": {"schemes": ["HMAC-SHA256"]}}}
    assert should_sign(_seller(supported), "sync_creatives", webhook_args)


def test_mcp_tool_call_parsing() -> None:
    body = json.dumps({"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "x", "arguments": {"a": 1}}})
    assert mcp_tool_call(body.encode()) == ("x", {"a": 1})
    assert mcp_tool_call(b'{"method":"initialize"}') is None
    assert mcp_tool_call(b"not json") is None


async def test_signing_auth_signs_only_selected_tool_calls() -> None:
    key: SigningKey = generate_signing_key("ed25519", "hook-kid")
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"ok": True})

    auth = AdcpSigningAuth(key, lambda name, _args: name == "create_media_buy")
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler), auth=auth) as client:
        signed_body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "create_media_buy", "arguments": {}}}
        await client.post("https://seller.example/mcp", json=signed_body)
        await client.post("https://seller.example/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "initialize"})
        other = {**signed_body, "params": {"name": "get_products", "arguments": {}}}
        await client.post("https://seller.example/mcp", json=other)

    first, init, read = seen
    assert "signature" in first.headers and "content-digest" in first.headers
    assert "signature" not in init.headers and "signature" not in read.headers
    components, params = _parse_components(first.headers["signature-input"])
    base = signature_base("POST", str(first.url), dict(first.headers), components, params)
    _verify_with_jwk(key.public_jwk(), base.encode(), _sig_bytes(first.headers["signature"]))
    assert first.headers["content-digest"] == content_digest(first.content)


async def test_fastmcp_transport_sends_verifiable_signature() -> None:
    """End to end: fastmcp StreamableHttpTransport -> our auth hook -> seller ASGI app."""
    from fastmcp import Client, FastMCP
    from fastmcp.client.transports import StreamableHttpTransport

    received: list[tuple[dict[str, str], bytes]] = []
    seller_mcp = FastMCP("seller")

    @seller_mcp.tool(name="create_media_buy")
    async def create_media_buy(idempotency_key: str) -> dict[str, str]:
        return {"status": "completed", "media_buy_id": "mb_1"}

    seller_app = seller_mcp.http_app(path="/mcp", stateless_http=True, json_response=True)

    async def spy(scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await seller_app(scope, receive, send)
            return
        chunks: list[bytes] = []

        async def recording_receive() -> dict:
            message = await receive()
            chunks.append(message.get("body", b""))
            return message

        headers = {k.decode(): v.decode() for k, v in scope["headers"]}
        await seller_app(scope, recording_receive, send)
        received.append((headers, b"".join(chunks)))

    key = generate_signing_key("ed25519", "e2e-kid")
    auth = AdcpSigningAuth(key, lambda name, _args: name == "create_media_buy")

    def factory(**kwargs: Any) -> httpx2.AsyncClient:
        kwargs.pop("verify", None)
        return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=spy), **kwargs)

    async with seller_app.router.lifespan_context(seller_app):
        transport = StreamableHttpTransport(
            "https://seller.example/mcp", headers={"Authorization": "Bearer t"}, auth=auth, httpx_client_factory=factory
        )
        async with Client(transport=transport) as client:
            result = await client.call_tool("create_media_buy", {"idempotency_key": "nxflo-0123456789abcdef"})
    assert result.structured_content["media_buy_id"] == "mb_1"

    signed = [(h, b) for h, b in received if "signature" in h]
    assert len(signed) == 1 and any("signature" not in h for h, _ in received)
    headers, body = signed[0]
    assert headers["content-digest"] == content_digest(body)
    components, params = _parse_components(headers["signature-input"])
    base = signature_base("POST", "https://seller.example/mcp", headers, components, params)
    _verify_with_jwk(key.public_jwk(), base.encode(), _sig_bytes(headers["signature"]))
