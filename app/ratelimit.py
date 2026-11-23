"""In-memory sliding-window rate limiter (stdlib only).

Good enough for a single-process deployment (the shipped Docker setup).
Behind several workers or replicas each process keeps its own counters,
so the effective limit is multiplied by the process count; move the
counters to a shared store (e.g. Redis) before scaling out.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable

from . import config


class RateLimiter:
    """Allow at most ``limit`` hits per ``window`` seconds for each key.

    The limit is re-read from ``COPILOT_<NAME>_RATE`` on every check, so a
    deployment (or a test) can change it without restarting the process.
    """

    def __init__(self, name: str, default: str, clock: Callable[[], float] = time.monotonic):
        self.name = name
        self.default = default
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _settings(self) -> tuple[int, float]:
        return config.rate(self.name, self.default)

    def _prune(self, key: str, now: float, window: float) -> deque[float]:
        hits = self._hits.get(key)
        if hits is None:
            hits = self._hits[key] = deque()
        while hits and hits[0] <= now - window:
            hits.popleft()
        return hits

    def retry_after(self, key: str) -> int:
        """Seconds until ``key`` may try again; 0 when it is not limited.
        Does not record a hit."""
        limit, window = self._settings()
        if limit <= 0:
            return 0
        with self._lock:
            now = self._clock()
            hits = self._prune(key, now, window)
            if len(hits) < limit:
                return 0
            return max(1, math.ceil(hits[0] + window - now))

    def hit(self, key: str) -> int:
        """Record one hit. Returns 0 when allowed, else the seconds to wait
        (the hit is then not recorded)."""
        limit, window = self._settings()
        if limit <= 0:
            return 0
        with self._lock:
            now = self._clock()
            hits = self._prune(key, now, window)
            if len(hits) >= limit:
                return max(1, math.ceil(hits[0] + window - now))
            hits.append(now)
            if len(self._hits) > 50_000:  # bound memory under key spraying
                for k in [k for k, v in self._hits.items() if not v]:
                    del self._hits[k]
            return 0

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)


# Failed logins per client IP + username (a correct password clears it).
login_failures = RateLimiter("login", "10/300")
# Failed logins per client IP across all usernames (credential stuffing).
login_failures_ip = RateLimiter("login_ip", "50/300")
# Registrations (new tenants) per client IP.
register_attempts = RateLimiter("register", "10/3600")
# Widget chat requests per widget key + client IP, and per widget key overall.
widget_chat = RateLimiter("widget_chat", "20/60")
widget_chat_key = RateLimiter("widget_chat_key", "600/60")

ALL = (login_failures, login_failures_ip, register_attempts, widget_chat, widget_chat_key)


def reset_all() -> None:
    for limiter in ALL:
        limiter.reset()
