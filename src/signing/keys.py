"""Signing keys and JWKS publication for AdCP request signing.

Keys come from configuration only (``NXFLO_SIGNING_PRIVATE_KEY_PATH`` or
``NXFLO_SIGNING_PRIVATE_KEY``, PEM PKCS#8 or a private JWK); nothing is committed.
Published JWKs carry ``use: "sig"``, ``key_ops: ["verify"]``,
``adcp_use: "request-signing"`` and the JWS ``alg`` name (EdDSA / ES256).
"""

import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from src.config import settings

ALG_ED25519 = "ed25519"
ALG_ES256 = "ecdsa-p256-sha256"
JWK_ALG = {ALG_ED25519: "EdDSA", ALG_ES256: "ES256"}
ADCP_USE = "request-signing"

PrivateKey = Ed25519PrivateKey | ec.EllipticCurvePrivateKey


class SigningKeyError(ValueError):
    """Signing key material is missing or unusable."""


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


@dataclass(frozen=True)
class SigningKey:
    """A private request-signing key with its key id and RFC 9421 algorithm."""

    kid: str
    alg: str
    private_key: PrivateKey

    def sign(self, data: bytes) -> bytes:
        """Raw signature bytes (Ed25519, or IEEE P1363 r||s for ECDSA P-256)."""
        if isinstance(self.private_key, Ed25519PrivateKey):
            return self.private_key.sign(data)
        der = self.private_key.sign(data, ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        return r.to_bytes(32, "big") + s.to_bytes(32, "big")

    def public_jwk(self) -> dict[str, Any]:
        """Public JWK suitable for JWKS publication (never contains ``d``)."""
        jwk: dict[str, Any] = {"kid": self.kid}
        if isinstance(self.private_key, Ed25519PrivateKey):
            raw = self.private_key.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            )
            jwk.update({"kty": "OKP", "crv": "Ed25519", "x": b64url(raw)})
        else:
            numbers = self.private_key.public_key().public_numbers()
            jwk.update(
                {
                    "kty": "EC",
                    "crv": "P-256",
                    "x": b64url(numbers.x.to_bytes(32, "big")),
                    "y": b64url(numbers.y.to_bytes(32, "big")),
                }
            )
        jwk.update(
            {"alg": JWK_ALG[self.alg], "use": "sig", "key_ops": ["verify"], "adcp_use": ADCP_USE}
        )
        return jwk

    def private_pem(self) -> bytes:
        return self.private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )


def _from_private_key(private_key: object, kid: str) -> SigningKey:
    if isinstance(private_key, Ed25519PrivateKey):
        return SigningKey(kid=kid, alg=ALG_ED25519, private_key=private_key)
    if isinstance(private_key, ec.EllipticCurvePrivateKey) and isinstance(
        private_key.curve, ec.SECP256R1
    ):
        return SigningKey(kid=kid, alg=ALG_ES256, private_key=private_key)
    raise SigningKeyError("only Ed25519 and ECDSA P-256 keys are allowed")


def _from_jwk(jwk: dict[str, Any], kid: str | None) -> SigningKey:
    key_id = kid or jwk.get("kid")
    if not key_id:
        raise SigningKeyError("signing key has no kid (set NXFLO_SIGNING_KEY_ID)")
    if "d" not in jwk:
        raise SigningKeyError("JWK has no private component 'd'")
    d = b64url_decode(str(jwk["d"]))
    if jwk.get("kty") == "OKP" and jwk.get("crv") == "Ed25519":
        return _from_private_key(Ed25519PrivateKey.from_private_bytes(d), str(key_id))
    if jwk.get("kty") == "EC" and jwk.get("crv") == "P-256":
        private = ec.derive_private_key(int.from_bytes(d, "big"), ec.SECP256R1())
        return _from_private_key(private, str(key_id))
    raise SigningKeyError("unsupported JWK (need OKP/Ed25519 or EC/P-256)")


def load_signing_key(material: str | bytes, kid: str | None = None) -> SigningKey:
    """Load a private key from PEM (PKCS#8) or private-JWK text."""
    text = material.decode("utf-8") if isinstance(material, bytes) else material
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            jwk = json.loads(stripped)
        except ValueError as exc:
            raise SigningKeyError(f"invalid JWK JSON: {exc}") from exc
        return _from_jwk(jwk, kid)
    if not kid:
        raise SigningKeyError("PEM keys need NXFLO_SIGNING_KEY_ID")
    try:
        private = serialization.load_pem_private_key(stripped.encode("utf-8"), password=None)
    except (ValueError, TypeError, UnsupportedAlgorithm) as exc:
        raise SigningKeyError(f"invalid PEM private key: {exc}") from exc
    return _from_private_key(private, kid)


def generate_signing_key(alg: str, kid: str) -> SigningKey:
    if alg == ALG_ED25519:
        return SigningKey(kid=kid, alg=alg, private_key=Ed25519PrivateKey.generate())
    if alg == ALG_ES256:
        return SigningKey(kid=kid, alg=alg, private_key=ec.generate_private_key(ec.SECP256R1()))
    raise SigningKeyError(f"unsupported algorithm {alg!r}")


def load_signing_key_from_settings() -> SigningKey | None:
    """The configured key, or None when no key material is configured."""
    kid = settings.signing_key_id or None
    if settings.signing_private_key:
        return load_signing_key(settings.signing_private_key, kid)
    if settings.signing_private_key_path:
        return load_signing_key(Path(settings.signing_private_key_path).read_text(), kid)
    return None


def _additional_public_keys() -> list[dict[str, Any]]:
    if not settings.signing_additional_jwks_path:
        return []
    data = json.loads(Path(settings.signing_additional_jwks_path).read_text())
    keys = data.get("keys", []) if isinstance(data, dict) else []
    public: list[dict[str, Any]] = []
    for key in keys:
        if isinstance(key, dict) and "d" not in key and key.get("kid"):
            public.append(key)
    return public


def build_jwks(key: SigningKey | None) -> dict[str, Any]:
    """JWKS for /.well-known/jwks.json: active key first, then rotation keys."""
    keys: list[dict[str, Any]] = [key.public_jwk()] if key else []
    seen = {k["kid"] for k in keys}
    for extra in _additional_public_keys():
        if extra["kid"] not in seen:
            keys.append(extra)
            seen.add(extra["kid"])
    return {"keys": keys}
