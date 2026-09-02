"""Jinja2 environment, template rendering, and shared view helpers."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

_HERE = Path(__file__).resolve().parent
TEMPLATE_DIR = _HERE / "templates"
STATIC_DIR = _HERE / "static"

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

_UNITS = ("B", "KB", "MB", "GB", "TB")


def humansize(n: int | None) -> str:
    if not n:
        return "0 B"
    size = float(n)
    for unit in _UNITS:
        if size < 1024 or unit == _UNITS[-1]:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def shortdate(value: datetime | None) -> str:
    return value.date().isoformat() if value else "—"


def datetimefmt(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value else "—"


_STATUS_RU = {"inbox": "в очереди", "tagged": "размечен", "archived": "архив"}


def statusfmt(value: object) -> str:
    key = getattr(value, "value", value)
    return _STATUS_RU.get(str(key), str(key))


templates.env.filters["humansize"] = humansize
templates.env.filters["shortdate"] = shortdate
templates.env.filters["datetimefmt"] = datetimefmt
templates.env.filters["statusfmt"] = statusfmt


def render(
    request: Request,
    name: str,
    context: dict[str, Any] | None = None,
    *,
    status_code: int = 200,
    toast: str | None = None,
) -> HTMLResponse:
    """Full page (``name``) or, for an ``HX-Request``, its ``partial`` block only.

    ``toast`` sets an ``HX-Trigger`` so the page shows a transient toast.
    """
    from app.web import csrf

    ctx = {
        "request": request,
        "user": getattr(request.state, "user", None),
        "csrf": csrf.issue(request),
        **(context or {}),
    }
    if request.headers.get("HX-Request") and ctx.get("partial"):
        name = ctx["partial"]
    resp = templates.TemplateResponse(request, name, ctx, status_code=status_code)
    if toast:
        import json

        resp.headers["HX-Trigger"] = json.dumps({"dc-toast": toast})
    return resp
