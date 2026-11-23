"""Runtime configuration read from environment variables.

Values are read at call time (not import time) so tests and the startup
check see the environment as it is when the app actually runs.
"""

from __future__ import annotations

import os

DEV_SECRET = "dev-only-change-me"
DEV_ENVS = frozenset({"dev", "development", "test"})


def environment() -> str:
    """``COPILOT_ENV`` (default ``production``)."""
    return os.environ.get("COPILOT_ENV", "production").strip().lower() or "production"


def is_dev() -> bool:
    return environment() in DEV_ENVS


def secret_key() -> str:
    """The JWT signing secret. Falls back to a dev-only value, which
    :func:`validate_settings` refuses outside ``COPILOT_ENV=dev``."""
    return os.environ.get("COPILOT_SECRET", "") or DEV_SECRET


def validate_settings() -> None:
    """Fail fast on unsafe configuration. Called on application startup."""
    if secret_key() == DEV_SECRET and not is_dev():
        raise RuntimeError(
            "COPILOT_SECRET is not set (or is the dev-only default). Set a long random"
            " COPILOT_SECRET, or set COPILOT_ENV=dev for local development only."
        )


def rate(name: str, default: str) -> tuple[int, float]:
    """Parse a ``"<count>/<seconds>"`` rate from ``COPILOT_<NAME>_RATE``.

    ``0/<seconds>`` (or a count of 0) disables that limiter.
    """
    raw = os.environ.get(f"COPILOT_{name.upper()}_RATE", default)
    try:
        count, seconds = raw.split("/", 1)
        return int(count), float(seconds)
    except ValueError as exc:
        raise RuntimeError(
            f"COPILOT_{name.upper()}_RATE must look like '10/60', got {raw!r}"
        ) from exc


def trust_proxy() -> bool:
    """Use the first ``X-Forwarded-For`` address as the client IP. Only
    enable behind a reverse proxy you control (it overwrites the header)."""
    return os.environ.get("COPILOT_TRUST_PROXY", "").strip().lower() in {"1", "true", "yes"}
