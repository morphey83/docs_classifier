"""Job dispatch: SAQ (Postgres) queue in production, BackgroundTask inline.

Job functions accept an optional first positional ``ctx`` (SAQ passes its
context dict there) plus keyword arguments, so the same callable works both
ways.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import BackgroundTasks

from app.config import settings

_queue = None


def get_queue():
    global _queue
    if _queue is None:
        from saq import Queue

        _queue = Queue.from_url(settings.sync_database_url, name="docs")
    return _queue


def _plain(value: Any) -> Any:
    return str(value) if isinstance(value, uuid.UUID) else value


async def dispatch(
    background: BackgroundTasks | None,
    name: str,
    fn: Callable[..., Awaitable[Any]],
    /,
    **kwargs: Any,
) -> None:
    if settings.job_mode == "inline":
        if background is not None:
            background.add_task(fn, **kwargs)
        else:
            # No FastAPI request in flight (e.g. the bot) — just run it now.
            await fn(**{k: _plain(v) for k, v in kwargs.items()})
        return
    await get_queue().enqueue(name, **{k: _plain(v) for k, v in kwargs.items()})
