from __future__ import annotations

import hashlib
import secrets

from nacl.encoding import Base64Encoder, HexEncoder
from nacl.signing import SigningKey


def _normalize_private_key(private_key_hex: str | None) -> bytes:
    if private_key_hex is None:
        return secrets.token_bytes(32)

    if len(private_key_hex) != 64:
        raise ValueError(
            "validator private key must be a 64-character hex string"
        )

    try:
        return bytes.fromhex(private_key_hex)
    except ValueError as exc:
        raise ValueError("validator private key must be valid hex") from exc


def generate_validator_material(private_key_hex: str | None = None) -> dict:
    seed = _normalize_private_key(private_key_hex)
    signing_key = SigningKey(seed=seed)
    verify_key = signing_key.verify_key

    priv_key_with_pub = signing_key.encode() + verify_key.encode()
    public_key_bytes = verify_key.encode()

    address_bytes = hashlib.sha256(public_key_bytes).digest()[:20]

    return {
        "validator_private_key_hex": signing_key.encode(
            encoder=HexEncoder
        ).decode("ascii"),
        "validator_public_key_hex": verify_key.encode(
            encoder=HexEncoder
        ).decode("ascii"),
        "priv_validator_key": {
            "address": address_bytes.hex().upper(),
            "pub_key": {
                "type": "tendermint/PubKeyEd25519",
                "value": verify_key.encode(encoder=Base64Encoder).decode(
                    "ascii"
                ),
            },
            "priv_key": {
                "type": "tendermint/PrivKeyEd25519",
                "value": Base64Encoder.encode(priv_key_with_pub).decode(
                    "ascii"
                ),
            },
        },
    }
