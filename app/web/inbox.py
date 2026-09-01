"""Web UI: "Очередь на сортировку" — the inbox across every domain you can tag."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.rbac import ROLE_CAPS, Cap, Role
from app.services import documents as docs_svc
from app.services import domains as domains_svc
from app.services import tags as tags_svc
from app.web.csrf import CsrfGuard
from app.web.deps import current_user
from app.web.search import _tags_by_doc
from app.web.templating import render

router = APIRouter()


async def _taggable_domains(db: AsyncSession, user):
    """(domain, role) pairs where the user may process the inbox (write cap)."""
    out = {}
    for domain, member in await domains_svc.list_memberships(db, user):
        if Cap.write in ROLE_CAPS[Role(member.role)]:
            out[domain.id] = domain
    return out


async def _doc_domain(db, user, document_id: uuid.UUID):
    doc = await docs_svc.get_document(db, document_id)
    if doc is None:
        return None, None
    doms = await _taggable_domains(db, user)
    return doc, doms.get(doc.domain_id)


@router.get("/inbox")
async def inbox_page(
    request: Request,
    user=Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    doms = await _taggable_domains(db, user)
    ids = list(doms)
    doc = await docs_svc.next_inbox_across(db, ids, user.id)
    tags = (await _tags_by_doc(db, [doc.id])).get(doc.id, []) if doc else []
    freq = []
    if doc is not None:
        freq = [t.name for t, _ in (await tags_svc.list_tags(db, doc.domain_id))[:12]]
    return render(
        request,
        "inbox.html",
        {
            "doc": doc,
            "doc_domain": doms.get(doc.domain_id).name if doc else None,
            "doc_tags": tags,
            "remaining": await docs_svc.inbox_count_across(db, ids),
            "freq_tags": freq,
        },
    )


@router.post("/inbox/{document_id}/done")
async def inbox_done(
    document_id: uuid.UUID,
    tags: str = Form(default=""),
    _: None = CsrfGuard,
    user=Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    doc, domain = await _doc_domain(db, user, document_id)
    if domain is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "нет прав на этот документ")
    names = [p.strip() for p in tags.split(",") if p.strip()]
    tag_ids = []
    for name in names:
        tag = await tags_svc.get_or_create_tag(db, domain.id, name, actor=user)
        tag_ids.append(tag.id)
    await tags_svc.set_document_tags(db, doc, tag_ids, actor=user)
    await docs_svc.complete_document(db, doc)
    return RedirectResponse("/inbox", status_code=303)


@router.post("/inbox/{document_id}/defer")
async def inbox_defer(
    document_id: uuid.UUID,
    _: None = CsrfGuard,
    user=Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    doc, domain = await _doc_domain(db, user, document_id)
    if domain is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "нет прав на этот документ")
    await docs_svc.defer_document(db, doc, user.id)
    return RedirectResponse("/inbox", status_code=303)


@router.post("/inbox/undefer")
async def inbox_undefer(
    _: None = CsrfGuard,
    user=Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    for domain_id in await _taggable_domains(db, user):
        await docs_svc.clear_defers(db, domain_id, user.id)
    return RedirectResponse("/inbox", status_code=303)
