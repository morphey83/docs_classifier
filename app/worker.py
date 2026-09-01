"""SAQ worker — runs on the Postgres queue (no Redis).

Real jobs (archive extraction, text parsing, OCR, indexing, artifact builds,
cleanup) arrive in later phases. Phase 0 ships only a trivial ``ping``.
"""

from __future__ import annotations

from typing import Any

from saq import Queue

from app.config import settings

queue = Queue.from_url(settings.sync_database_url, name="docs")


async def ping(ctx: dict[str, Any], *, value: str = "pong") -> str:
    return value


settings_dict = {
    "queue": queue,
    "functions": [ping],
    "concurrency": 4,
}
