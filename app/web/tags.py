"""Web UI: the global tag pool — searchable table, inline rename / recolour,
merge into another tag (§7 rev 2)."""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Tag, User
from app.services import tags as svc
from app.web.csrf import CsrfGuard
from app.web.deps import current_user
from app.web.templating import render

router = APIRouter()

_PER_PAGE = 20
_SWATCHES = ["", "#206bc4", "#4299e1", "#2fb344", "#f76707", "#d63939", "#ae3ec9", "#868e96"]


async def _load(db: AsyncSession, tag_id: uuid.UUID) -> Tag:
    tag = await db.get(Tag, tag_id)
    if tag is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "тег не найден")
    return tag


async def _table_ctx(db: AsyncSession, q: str, page: int) -> dict:
    rows = await svc.list_tags(db, q=q)
    total = len(rows)
    pages = max(1, -(-total // _PER_PAGE))
    page = min(max(1, page), pages)
    return {
        "partial": "_tags_table.html",
        "tags": rows[(page - 1) * _PER_PAGE : page * _PER_PAGE],
        "all_tags": await svc.list_tags(db),
        "swatches": _SWATCHES,
        "q": q,
        "page": page,
        "pages": pages,
        "total": total,
    }


def _int(raw: str | None) -> int:
    try:
        return max(1, int(raw or 1))
    except (TypeError, ValueError):
        return 1


def _toast(msg: str) -> Response:
    # HTTP headers are latin-1 — json.dumps escapes the Cyrillic to \uXXXX
    return Response(status_code=204, headers={"HX-Trigger": json.dumps({"dc-toast": msg})})


@router.get("/tags")
async def tags_page(
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    q = (request.query_params.get("q") or "").strip()
    ctx = await _table_ctx(db, q, _int(request.query_params.get("page")))
    return render(request, "tags.html", ctx)


@router.post("/tags/{tag_id}/rename")
async def tag_rename(
    tag_id: uuid.UUID,
    name: str = Form(...),
    _: None = CsrfGuard,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    tag = await _load(db, tag_id)
    try:
        await svc.rename_tag(db, tag, name)
    except svc.TagError as err:
        raise HTTPException(status.HTTP_409_CONFLICT, str(err)) from err
    return _toast("Название сохранено")


@router.post("/tags/{tag_id}/color")
async def tag_color(
    tag_id: uuid.UUID,
    color: str = Form(default=""),
    _: None = CsrfGuard,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    await svc.recolor_tag(db, await _load(db, tag_id), color or None)
    return _toast("Цвет сохранён")


@router.post("/tags/{tag_id}/merge")
async def tag_merge(
    request: Request,
    tag_id: uuid.UUID,
    into: uuid.UUID = Form(...),
    q: str = Form(default=""),
    _: None = CsrfGuard,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    source = await _load(db, tag_id)
    target = await _load(db, into)
    try:
        await svc.merge_tags(db, source=source, target=target, owner_id=user.id)
    except svc.TagError as err:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(err)) from err
    ctx = await _table_ctx(db, q.strip(), 1)
    return render(request, "tags.html", ctx, toast=f"Слито в «{target.name}»")


# legacy single-form edit endpoint kept for the API-less callers / old bookmarks
@router.post("/tags/{tag_id}")
async def tag_edit(
    tag_id: uuid.UUID,
    name: str = Form(...),
    color: str = Form(default=""),
    _: None = CsrfGuard,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    tag = await _load(db, tag_id)
    try:
        await svc.rename_tag(db, tag, name)
        await svc.recolor_tag(db, tag, color or None)
    except svc.TagError as err:
        raise HTTPException(status.HTTP_409_CONFLICT, str(err)) from err
    return RedirectResponse("/tags", status_code=303)
