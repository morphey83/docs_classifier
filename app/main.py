"""FastAPI application factory."""

from __future__ import annotations

import json
import logging
import tempfile
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__
from app.api import api_router
from app.api import docsets as _docsets
from app.api import health as _health
from app.api import tglink as _tglink
from app.config import settings
from app.db import dispose_engine
from app.web import web_router
from app.web.deps import AuthRequired
from app.web.templating import STATIC_DIR, render

log = logging.getLogger("app.web")

_ERR_TITLES = {
    400: "Неверный запрос",
    403: "Доступ запрещён",
    404: "Страница не найдена",
    405: "Метод не разрешён",
    413: "Файл слишком большой",
    422: "Проверьте введённые данные",
    500: "Что-то пошло не так",
    502: "Сервис недоступен",
    503: "Сервис недоступен",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    # keep multipart spooling (large uploads) on the data volume, not on a
    # possibly RAM-backed /tmp
    upload_tmp = settings.data_dir / "tmp"
    upload_tmp.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(upload_tmp)
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

    def _wants_json(request: Request) -> bool:
        p = request.url.path
        if p.startswith(("/api", "/health")):
            return True
        accept = request.headers.get("accept", "")
        return "application/json" in accept and "text/html" not in accept

    _GENERIC = {
        "Not Found", "Forbidden", "Unauthorized", "Bad Request",
        "Method Not Allowed", "Internal Server Error", "Unprocessable Entity",
    }

    def _error_response(request: Request, code: int, detail: object) -> Response:
        detail = str(detail).strip() if detail else ""
        if request.headers.get("HX-Request"):
            detail = detail or _ERR_TITLES.get(code, "Ошибка")
            # don't blow up the page — show the message as a toast, swap nothing
            resp = Response(status_code=200)
            resp.headers["HX-Reswap"] = "none"
            resp.headers["HX-Trigger"] = json.dumps({"dc-toast": detail})
            return resp
        return render(
            request,
            "error.html",
            {
                "code": code,
                "title": _ERR_TITLES.get(code, "Ошибка"),
                "detail": "" if detail in _GENERIC else detail,
                "back": request.headers.get("referer") or "/search",
            },
            status_code=code,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_exc(request: Request, exc: StarletteHTTPException):
        if _wants_json(request):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        return _error_response(request, exc.status_code, exc.detail)

    @app.exception_handler(RequestValidationError)
    async def _validation_exc(request: Request, exc: RequestValidationError):
        if _wants_json(request):
            return JSONResponse(
                {"detail": jsonable_encoder(exc.errors())}, status_code=422
            )
        return _error_response(request, 422, "Форма заполнена неверно.")

    if not settings.debug:

        @app.exception_handler(Exception)
        async def _unhandled(request: Request, exc: Exception):
            log.exception("unhandled error on %s %s", request.method, request.url.path)
            if _wants_json(request):
                return JSONResponse({"detail": "internal server error"}, status_code=500)
            return _error_response(request, 500, "Внутренняя ошибка сервера.")

    return app


app = create_app()
