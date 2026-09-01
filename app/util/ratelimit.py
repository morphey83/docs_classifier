"""A tiny in-process fixed-window rate limiter.

Good enough for the single-container VDS deployment: it guards the public
``GET /d/{token}`` endpoint against a hammering client. Not shared across
processes; a real deployment behind several workers would move this to Postgres.
"""

from __future__ import annotations

import time

_hits: dict[str, list[float]] = {}


def check(key: str, limit: int, window_s: float = 60.0) -> bool:
    """Return ``True`` if ``key`` is still under ``limit`` hits in the window."""
    if limit <= 0:
        return True
    now = time.monotonic()
    bucket = [t for t in _hits.get(key, ()) if now - t < window_s]
    if len(bucket) >= limit:
        _hits[key] = bucket
        return False
    bucket.append(now)
    _hits[key] = bucket
    return True


def reset() -> None:
    _hits.clear()
