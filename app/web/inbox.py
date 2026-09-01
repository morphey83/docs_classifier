"""Web UI: the inbox — process documents one card at a time."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.rbac import Cap
from app.services import documents as docs_svc
from app.services import tags as tags_svc
from app.web.csrf import CsrfGuard
from app.web.deps import DomainView, domain_by_slug, require_cap
from app.web.search import _tags_by_doc
from app.web.templating import render

router = APIRouter()


@router.get("/domains/{slug}/inbox")
async def inbox_page(
    request: Request,
    view: DomainView = Depends(domain_by_slug),
    db: AsyncSession = Depends(get_session),
) -> Response:
    require_cap(view, Cap.write)
    user = request.state.user
    doc = await docs_svc.next_inbox_document(db, view.domain.id, user.id)
    tags = (await _tags_by_doc(db, [doc.id])).get(doc.id, []) if doc else []
    remaining = await docs_svc.inbox_count(db, view.domain.id)
    freq = await tags_svc.list_tags(db, view.domain.id)
    return render(
        request,
        "inbox.html",
        {
            "view": view,
            "doc": doc,
            "doc_tags": tags,
            "remaining": remaining,
            "freq_tags": [t.name for t, _ in freq[:12]],
        },
    )


@router.post("/domains/{slug}/inbox/{document_id}/done")
async def inbox_done(
    request: Request,
    document_id: uuid.UUID,
    tags: str = Form(default=""),
    _: None = CsrfGuard,
    view: DomainView = Depends(domain_by_slug),
    db: AsyncSession = Depends(get_session),
) -> Response:
    require_cap(view, Cap.write)
    user = request.state.user
    doc = await docs_svc.get_document(db, document_id)
    if doc is not None and doc.domain_id == view.domain.id:
        names = [p.strip() for p in tags.split(",") if p.strip()]
        tag_ids = []
        for name in names:
            tag = await tags_svc.get_or_create_tag(db, view.domain.id, name, actor=user)
            tag_ids.append(tag.id)
        await tags_svc.set_document_tags(db, doc, tag_ids, actor=user)
        await docs_svc.complete_document(db, doc)
    return RedirectResponse(f"/domains/{view.domain.slug}/inbox", status_code=303)


@router.post("/domains/{slug}/inbox/{document_id}/defer")
async def inbox_defer(
    request: Request,
    document_id: uuid.UUID,
    _: None = CsrfGuard,
    view: DomainView = Depends(domain_by_slug),
    db: AsyncSession = Depends(get_session),
) -> Response:
    require_cap(view, Cap.write)
    doc = await docs_svc.get_document(db, document_id)
    if doc is not None and doc.domain_id == view.domain.id:
        await docs_svc.defer_document(db, doc, request.state.user.id)
    return RedirectResponse(f"/domains/{view.domain.slug}/inbox", status_code=303)


@router.post("/domains/{slug}/inbox/undefer")
async def inbox_undefer(
    request: Request,
    _: None = CsrfGuard,
    view: DomainView = Depends(domain_by_slug),
    db: AsyncSession = Depends(get_session),
) -> Response:
    require_cap(view, Cap.write)
    await docs_svc.clear_defers(db, view.domain.id, request.state.user.id)
    return RedirectResponse(f"/domains/{view.domain.slug}/inbox", status_code=303)
