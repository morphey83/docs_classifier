"""Web UI: the global tag pool — list, rename, recolour, merge (§7 rev 2)."""

from __future__ import annotations

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


async def _load(db: AsyncSession, tag_id: uuid.UUID) -> Tag:
    tag = await db.get(Tag, tag_id)
    if tag is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "тег не найден")
    return tag


@router.get("/tags")
async def tags_page(
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    return render(request, "tags.html", {"tags": await svc.list_tags(db)})


@router.post("/tags/{tag_id}")
async def tag_edit(
    request: Request,
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


@router.post("/tags/{tag_id}/merge")
async def tag_merge(
    request: Request,
    tag_id: uuid.UUID,
    into: uuid.UUID = Form(...),
    _: None = CsrfGuard,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    source = await _load(db, tag_id)
    target = await _load(db, into)
    try:
        await svc.merge_tags(db, source=source, target=target)
    except svc.TagError as err:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(err)) from err
    return RedirectResponse("/tags", status_code=303)
