"""Authentication primitives: password hashing and JWT handling.

Passwords use PBKDF2-HMAC-SHA256 with a per-user random salt (stdlib only,
no extra dependency). Tokens are signed HS256 JWTs built with the stdlib
``hmac``/``base64``/``json`` modules, with strict expiry checking and a
constant-time signature comparison.

The token carries the tenant context (``tenant_id`` + ``role``) so every
request is authenticated AND tenant-scoped in one step — the tenant id is
never trusted from the request body or URL.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from typing import Any

SECRET_KEY = os.environ.get("COPILOT_SECRET", "dev-only-change-me")
TOKEN_TTL_SECONDS = 24 * 3600


def hash_password(password: str) -> str:
    """Return ``salt$hash`` (both hex) for storage."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split("$", 1)
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt_hex), 200_000
    )
    return hmac.compare_digest(digest.hex(), digest_hex)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def create_token(user_id: int, username: str, tenant_id: int, role: str) -> str:
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64url(
        json.dumps(
            {
                "sub": user_id,
                "username": username,
                "tenant_id": tenant_id,
                "role": role,
                "exp": int(time.time()) + TOKEN_TTL_SECONDS,
            }
        ).encode()
    )
    signature = _b64url(
        hmac.new(SECRET_KEY.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    )
    return f"{header}.{payload}.{signature}"


class AuthError(ValueError):
    """Raised for any malformed, tampered or expired token."""


def decode_token(token: str) -> dict[str, Any]:
    try:
        header_b64, payload_b64, signature = token.split(".")
        expected = _b64url(
            hmac.new(
                SECRET_KEY.encode(), f"{header_b64}.{payload_b64}".encode(), hashlib.sha256
            ).digest()
        )
        if not hmac.compare_digest(signature, expected):
            raise AuthError("bad signature")
        payload = json.loads(_b64url_decode(payload_b64))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise AuthError("malformed token") from exc
    if payload.get("exp", 0) < int(time.time()):
        raise AuthError("expired token")
    return payload


def slugify(name: str) -> str:
    """Turn a tenant name into a URL-safe slug, e.g. 'Acme Support' -> 'acme-support'."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "tenant"
