"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.api import api_router
from app.config import settings
from app.db import dispose_engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    queue = None
    if settings.job_mode == "queue":
        from app.jobs import get_queue

        queue = get_queue()
        await queue.connect()
    yield
    if queue is not None:
        await queue.disconnect()
    await dispose_engine()


def create_app() -> FastAPI:
    app = FastAPI(
        title="DocsClassifier",
        version=__version__,
        debug=settings.debug,
        lifespan=lifespan,
    )
    app.include_router(api_router)
    return app


app = create_app()
