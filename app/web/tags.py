"""Web UI: per-domain tag vocabulary management."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Tag
from app.rbac import Cap
from app.services import tags as svc
from app.web.csrf import CsrfGuard
from app.web.deps import DomainView, domain_by_slug, require_cap
from app.web.templating import render

router = APIRouter()


async def _load(db: AsyncSession, view: DomainView, tag_id: uuid.UUID) -> Tag:
    tag = await db.get(Tag, tag_id)
    if tag is None or tag.domain_id != view.domain.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "тег не найден")
    return tag


@router.get("/domains/{slug}/tags")
async def tags_page(
    request: Request,
    view: DomainView = Depends(domain_by_slug),
    db: AsyncSession = Depends(get_session),
) -> Response:
    rows = await svc.list_tags(db, view.domain.id)
    return render(
        request,
        "tags.html",
        {"view": view, "tags": list(rows)},
    )


@router.post("/domains/{slug}/tags")
async def tag_create(
    request: Request,
    name: str = Form(...),
    color: str = Form(default=""),
    _: None = CsrfGuard,
    view: DomainView = Depends(domain_by_slug),
    db: AsyncSession = Depends(get_session),
) -> Response:
    require_cap(view, Cap.write)
    try:
        await svc.create_tag(
            db, view.domain.id, name=name, color=color or None, description=None,
            actor=request.state.user,
        )
    except svc.TagError as err:
        raise HTTPException(status.HTTP_409_CONFLICT, str(err)) from err
    return RedirectResponse(f"/domains/{view.domain.slug}/tags", status_code=303)


@router.post("/domains/{slug}/tags/{tag_id}")
async def tag_update(
    tag_id: uuid.UUID,
    name: str = Form(...),
    color: str = Form(default=""),
    _: None = CsrfGuard,
    view: DomainView = Depends(domain_by_slug),
    db: AsyncSession = Depends(get_session),
) -> Response:
    require_cap(view, Cap.manage)
    tag = await _load(db, view, tag_id)
    try:
        await svc.update_tag(db, tag, name=name, color=color or None, description=None)
    except svc.TagError as err:
        raise HTTPException(status.HTTP_409_CONFLICT, str(err)) from err
    return RedirectResponse(f"/domains/{view.domain.slug}/tags", status_code=303)


@router.post("/domains/{slug}/tags/{tag_id}/delete")
async def tag_delete(
    tag_id: uuid.UUID,
    _: None = CsrfGuard,
    view: DomainView = Depends(domain_by_slug),
    db: AsyncSession = Depends(get_session),
) -> Response:
    require_cap(view, Cap.manage)
    await svc.delete_tag(db, await _load(db, view, tag_id))
    return RedirectResponse(f"/domains/{view.domain.slug}/tags", status_code=303)


@router.post("/domains/{slug}/tags/{tag_id}/merge")
async def tag_merge(
    tag_id: uuid.UUID,
    into: uuid.UUID = Form(...),
    _: None = CsrfGuard,
    view: DomainView = Depends(domain_by_slug),
    db: AsyncSession = Depends(get_session),
) -> Response:
    require_cap(view, Cap.manage)
    source = await _load(db, view, tag_id)
    target = await _load(db, view, into)
    try:
        await svc.merge_tags(db, source=source, target=target)
    except svc.TagError as err:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(err)) from err
    return RedirectResponse(f"/domains/{view.domain.slug}/tags", status_code=303)
