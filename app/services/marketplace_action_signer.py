"""Canonical install-action signing shared by marketplace operations."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import jwt


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def canonical_payload_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def build_action_jwt(
    *,
    seller_id: str,
    install_id: str,
    action: str,
    payload_hash: str,
    private_key,
    hash_claim: str = "payload_hash",
) -> str:
    now = datetime.now(timezone.utc)
    if hash_claim not in {"payload_hash", "metadata_hash"}:
        raise ValueError("unsupported payload hash claim")
    claims = {
        "sub": seller_id,
        "iss": install_id,
        "action": action,
        "exp": now.timestamp() + 300,
        "iat": now.timestamp(),
        "jti": str(uuid4()),
    }
    claims[hash_claim] = payload_hash
    return jwt.encode(
        claims,
        private_key,
        algorithm="EdDSA",
    )


def sign_receipt_payload(payload: dict[str, Any], private_key) -> str:
    """Return a detached Ed25519 signature over canonical transmitted bytes."""
    return base64.b64encode(private_key.sign(canonical_json_bytes(payload))).decode("ascii")
