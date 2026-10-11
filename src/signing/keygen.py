"""Generate an AdCP request-signing keypair.

Usage:
    python -m src.signing.keygen --kid nxflo-buyer-2026-10 --private-out ./signing-key.pem
    python -m src.signing.keygen --alg ecdsa-p256-sha256 --kid k2 --private-out k2.pem \
        --public-out k2-jwks.json

The private key is written PEM/PKCS#8 with mode 0600 and O_EXCL (an existing file is
never overwritten). The public JWKS is printed (or written with --public-out).
Never commit the private key; point NXFLO_SIGNING_PRIVATE_KEY_PATH at it instead.
"""

import argparse
import json
import os
import sys

from src.signing.keys import ALG_ED25519, ALG_ES256, build_jwks, generate_signing_key


def _write_private(path: str, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--alg", choices=[ALG_ED25519, ALG_ES256], default=ALG_ED25519)
    parser.add_argument("--kid", required=True)
    parser.add_argument("--private-out", required=True)
    parser.add_argument("--public-out")
    args = parser.parse_args(argv)

    key = generate_signing_key(args.alg, args.kid)
    _write_private(args.private_out, key.private_pem())
    jwks = json.dumps({"keys": build_jwks(key)["keys"][:1]}, indent=2)
    if args.public_out:
        with open(args.public_out, "w", encoding="utf-8") as handle:
            handle.write(jwks + "\n")
    else:
        sys.stdout.write(jwks + "\n")
    sys.stderr.write(f"private key written to {args.private_out} (mode 0600)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
