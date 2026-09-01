"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api import api_router
from app.api import docsets as _docsets
from app.api import health as _health
from app.api import tglink as _tglink
from app.config import settings
from app.db import dispose_engine
from app.web import web_router
from app.web.deps import AuthRequired
from app.web.templating import STATIC_DIR


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
    app.include_router(api_router, prefix="/api")
    app.include_router(_health.router)  # /health at the root, for ops probes
    app.include_router(_tglink.router)  # /tg/link/* — the linking page + companions
    app.include_router(_docsets.public_router)  # /d/{token} — public share downloads
    app.include_router(web_router)  # the HTMX + Jinja UI, at /
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.exception_handler(AuthRequired)
    async def _redirect_to_login(request: Request, exc: AuthRequired):
        nxt = quote(exc.next_url, safe="")
        return RedirectResponse(f"/login?next={nxt}", status_code=303)

    return app


app = create_app()
