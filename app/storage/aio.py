"""Async helpers over the (sync) :class:`ObjectStore` interface."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from starlette.concurrency import run_in_threadpool

from app.storage.base import ObjectStore


@asynccontextmanager
async def fetch_local(store: ObjectStore, key: str) -> AsyncIterator[Path]:
    """``async with`` yielding a real local path to ``key``'s content.

    Wraps :meth:`ObjectStore.open_local`. On the local backend this is the
    stored file (no copy, no thread hop). On a remote backend the download —
    and the cleanup of the temp copy — run in a worker thread so the event
    loop is never blocked. Raises :class:`~app.storage.base.ObjectNotFound`
    if the key is absent.
    """
    cm = store.open_local(key)
    path = await run_in_threadpool(cm.__enter__)
    try:
        yield path
    except BaseException as exc:
        if not await run_in_threadpool(cm.__exit__, type(exc), exc, exc.__traceback__):
            raise
    else:
        await run_in_threadpool(cm.__exit__, None, None, None)
