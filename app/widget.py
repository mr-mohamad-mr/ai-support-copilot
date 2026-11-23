"""Public widget keys and the CORS allowlist for the embeddable widget.

A widget key is a *public* credential: it sits in the page source of the
tenant's website. It therefore grants exactly one thing, asking ``POST
/chat`` questions for its tenant, and nothing else (documents, users,
conversations and analytics all require a signed-in user's JWT). Abuse is
contained by an origin allowlist (enforced for browsers through CORS), a
rate limit per key + client IP and per key overall, and rotation.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from urllib.parse import urlsplit

KEY_PREFIX = "wk_"
_DEFAULT_PORTS = {"http": 80, "https": 443}

ALLOWED_METHODS = "POST"
ALLOWED_HEADERS = frozenset({"content-type", "x-widget-key"})


def new_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(24)


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def display_prefix(key: str) -> str:
    return key[: len(KEY_PREFIX) + 6] + "…"


def normalize_origin(value: str) -> str:
    """Return the canonical ``scheme://host[:port]`` form of an origin, the
    way browsers send it in the ``Origin`` header. Raises ``ValueError``
    for anything that is not a plain http(s) origin (paths, wildcards,
    credentials, query strings)."""
    raw = value.strip()
    parts = urlsplit(raw)
    if parts.scheme not in _DEFAULT_PORTS or not parts.hostname:
        raise ValueError(f"not an http(s) origin: {value!r}")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError(f"an origin has no path, query or fragment: {value!r}")
    if parts.username or parts.password or "*" in raw:
        raise ValueError(f"credentials and wildcards are not allowed: {value!r}")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"bad port in origin: {value!r}") from exc
    host = parts.hostname.lower()
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    if port is None or port == _DEFAULT_PORTS[parts.scheme]:
        return f"{parts.scheme}://{host}"
    return f"{parts.scheme}://{host}:{port}"


def tenant_for_key(db: sqlite3.Connection, key: str) -> int | None:
    if not key.startswith(KEY_PREFIX) or len(key) > 100:
        return None
    row = db.execute(
        "SELECT tenant_id FROM widget_keys WHERE key_hash = ?", (hash_key(key),)
    ).fetchone()
    return row["tenant_id"] if row else None


def allowed_origins(db: sqlite3.Connection, tenant_id: int) -> list[str]:
    rows = db.execute(
        "SELECT origin FROM widget_origins WHERE tenant_id = ? ORDER BY origin", (tenant_id,)
    ).fetchall()
    return [r["origin"] for r in rows]


def set_origins(db: sqlite3.Connection, tenant_id: int, origins: list[str]) -> list[str]:
    """Replace the tenant's allowlist (caller commits). Raises ValueError."""
    normalized = sorted({normalize_origin(o) for o in origins if o.strip()})
    db.execute("DELETE FROM widget_origins WHERE tenant_id = ?", (tenant_id,))
    db.executemany(
        "INSERT INTO widget_origins (tenant_id, origin) VALUES (?, ?)",
        [(tenant_id, o) for o in normalized],
    )
    return normalized


def _canonical(origin: str) -> str | None:
    try:
        return normalize_origin(origin)
    except ValueError:
        return None


def is_same_origin(origin: str, host_header: str | None) -> bool:
    """The page is served by this app itself (e.g. the /widget demo)."""
    if not host_header:
        return False
    return urlsplit(origin).netloc.lower() == host_header.strip().lower()


def origin_allowed_for_tenant(
    db: sqlite3.Connection, tenant_id: int, origin: str, host_header: str | None
) -> bool:
    if is_same_origin(origin, host_header):
        return True
    canonical = _canonical(origin)
    if canonical is None:
        return False
    return (
        db.execute(
            "SELECT 1 FROM widget_origins WHERE tenant_id = ? AND origin = ?",
            (tenant_id, canonical),
        ).fetchone()
        is not None
    )


def origin_allowed_for_any_tenant(
    db: sqlite3.Connection, origin: str, host_header: str | None
) -> bool:
    """Preflight check. A CORS preflight carries no credentials, so the
    tenant is unknown; the origin must be on *some* tenant's allowlist. The
    real request is then checked against its own key's allowlist."""
    if is_same_origin(origin, host_header):
        return True
    canonical = _canonical(origin)
    if canonical is None:
        return False
    return (
        db.execute("SELECT 1 FROM widget_origins WHERE origin = ? LIMIT 1", (canonical,)).fetchone()
        is not None
    )


def cors_headers(origin: str) -> dict[str, str]:
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Expose-Headers": "Retry-After",
        "Vary": "Origin",
    }
