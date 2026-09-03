"""Web UI: the card-by-card tagging modal used by the /search inbox preset
(open one, tag it, next). The queue itself is now the search preset; this
module is just the modal flow (docs/architecture.md §14 rev 2)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import DocStatus, Document, InboxDefer
from app.rbac import ROLE_CAPS, Cap, Role
from app.services import documents as docs_svc
from app.services import domains as domains_svc
from app.services import tags as tags_svc
from app.services import thumbs
from app.web.csrf import CsrfGuard
from app.web.deps import current_user
from app.web.search import _tags_by_doc
from app.web.templating import templates

router = APIRouter()


async def _taggable(db: AsyncSession, user) -> dict[uuid.UUID, object]:
    return {
        d.id: d
        for d, m in await domains_svc.list_memberships(db, user)
        if Cap.write in ROLE_CAPS[Role(m.role)]
    }


def _scope(doms: dict, raw: str | None) -> list[uuid.UUID]:
    if raw:
        try:
            did = uuid.UUID(raw)
        except ValueError:
            did = None
        if did in doms:
            return [did]
    return list(doms)


async def _queue(db: AsyncSession, domain_ids: list[uuid.UUID], user_id: uuid.UUID):
    if not domain_ids:
        return []
    deferred = select(InboxDefer.document_id).where(InboxDefer.user_id == user_id)
    rows = await db.scalars(
        select(Document)
        .where(
            Document.domain_id.in_(domain_ids),
            Document.status == DocStatus.inbox,
            Document.deleted_at.is_(None),
            Document.id.not_in(deferred),
        )
        .order_by(Document.uploaded_at.asc())
        .limit(500)
    )
    return list(rows)


@router.get("/inbox")
async def inbox_redirect() -> Response:
    return RedirectResponse("/search?preset=inbox", status_code=307)


def _csrf(request: Request) -> str:
    from app.web import csrf

    return csrf.issue(request)


async def _card_ctx(db: AsyncSession, user, doc: Document | None, raw_domain: str | None) -> dict:
    doms = await _taggable(db, user)
    domain = doms.get(doc.domain_id) if doc else None
    freq: list[str] = []
    tags: list[str] = []
    if doc is not None:
        freq = [n for n, _ in await tags_svc.suggest_tags(db, [doc.domain_id])]
        tags = (await _tags_by_doc(db, [doc.id])).get(doc.id, [])
    return {
        "doc": doc,
        "domain": domain,
        "freq": freq,
        "cur_tags": tags,
        "is_image": bool(doc and thumbs.can_thumb(doc.mime, doc.ext)),
        "picked": raw_domain or "",
    }


async def _next_doc(db, user, raw_domain):
    doms = await _taggable(db, user)
    ids = _scope(doms, raw_domain)
    q = await _queue(db, ids, user.id)
    return q[0] if q else None


@router.get("/inbox/card")
async def inbox_card(
    request: Request,
    doc: uuid.UUID | None = None,
    domain_id: str | None = None,
    user=Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    doms = await _taggable(db, user)
    target = None
    if doc is not None:
        d = await docs_svc.get_document(db, doc)
        if d is not None and d.domain_id in doms and d.status == DocStatus.inbox:
            target = d
    if target is None:
        target = await _next_doc(db, user, domain_id)
    ctx = await _card_ctx(db, user, target, domain_id)
    return templates.TemplateResponse(
        request, "_inbox_card.html", {**ctx, "csrf": _csrf(request)}
    )


async def _doc_domain(db, user, document_id):
    d = await docs_svc.get_document(db, document_id)
    doms = await _taggable(db, user)
    return (d, doms.get(d.domain_id)) if d is not None else (None, None)


async def _card_response(request, db, user, domain_id) -> Response:
    target = await _next_doc(db, user, domain_id)
    ctx = await _card_ctx(db, user, target, domain_id)
    resp = templates.TemplateResponse(
        request, "_inbox_card.html", {**ctx, "csrf": _csrf(request)}
    )
    resp.headers["HX-Trigger"] = "inbox-refresh"
    return resp


@router.post("/inbox/{document_id}/done")
async def inbox_done(
    request: Request,
    document_id: uuid.UUID,
    tags: str = Form(default=""),
    title: str = Form(default=""),
    domain_id: str = Form(default=""),
    _: None = CsrfGuard,
    user=Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    doc, domain = await _doc_domain(db, user, document_id)
    if domain is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "нет прав на этот документ")
    if title.strip():
        await docs_svc.update_document(db, doc, title=title)
    names = [p.strip() for p in tags.split(",") if p.strip()]
    tag_ids = await tags_svc.resolve_names(db, names, actor=user)
    await tags_svc.set_document_tags(db, doc, tag_ids, actor=user)
    await docs_svc.complete_document(db, doc)
    await db.flush()
    return await _card_response(request, db, user, domain_id or None)


@router.post("/inbox/{document_id}/defer")
async def inbox_defer(
    request: Request,
    document_id: uuid.UUID,
    domain_id: str = Form(default=""),
    _: None = CsrfGuard,
    user=Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    doc, domain = await _doc_domain(db, user, document_id)
    if domain is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "нет прав на этот документ")
    await docs_svc.defer_document(db, doc, user.id)
    await db.flush()
    return await _card_response(request, db, user, domain_id or None)


@router.post("/inbox/undefer")
async def inbox_undefer(
    _: None = CsrfGuard,
    user=Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    for domain_id in await _taggable(db, user):
        await docs_svc.clear_defers(db, domain_id, user.id)
    return RedirectResponse("/search?preset=inbox", status_code=303)
