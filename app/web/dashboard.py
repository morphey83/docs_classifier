"""Web UI: the "Домены" tab — a paginated table of the user's domains."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import DocStatus, Document, DomainMember, User
from app.rbac import ROLE_CAPS, Role
from app.services import domains as domains_svc
from app.web.csrf import CsrfGuard
from app.web.deps import current_user
from app.web.templating import render

router = APIRouter()
PAGE_SIZE = 20


async def _counts(db: AsyncSession, domain_ids: list) -> dict:
    """{domain_id: {"members": n, "total": n, "queue": n}} — 3 grouped queries."""
    out = {did: {"members": 0, "total": 0, "queue": 0} for did in domain_ids}
    if not domain_ids:
        return out
    for did, n in await db.execute(
        select(DomainMember.domain_id, func.count())
        .where(DomainMember.domain_id.in_(domain_ids))
        .group_by(DomainMember.domain_id)
    ):
        out[did]["members"] = n
    base = select(Document.domain_id, func.count()).where(
        Document.domain_id.in_(domain_ids), Document.deleted_at.is_(None)
    )
    for did, n in await db.execute(base.group_by(Document.domain_id)):
        out[did]["total"] = n
    for did, n in await db.execute(
        base.where(Document.status == DocStatus.inbox).group_by(Document.domain_id)
    ):
        out[did]["queue"] = n
    return out


@router.get("/")
async def dashboard(
    request: Request,
    page: int = Query(default=1, ge=1),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    rows = await domains_svc.list_memberships(db, user)  # sorted by name
    total = len(rows)
    pages = max(1, -(-total // PAGE_SIZE))
    page = min(page, pages)
    window = rows[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
    counts = await _counts(db, [d.id for d, _ in window])

    domains = [
        {
            "domain": d,
            "role": m.role,
            "caps": ROLE_CAPS[Role(m.role)],
            "members": counts[d.id]["members"],
            "total": counts[d.id]["total"],
            "queue": counts[d.id]["queue"],
        }
        for d, m in window
    ]
    return render(
        request,
        "dashboard.html",
        {"domains": domains, "page": page, "pages": pages, "total": total},
    )


@router.post("/domains")
async def create_domain(
    name: str = Form(...),
    _: None = CsrfGuard,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    name = name.strip()
    if not name:
        return RedirectResponse("/", status_code=303)
    domain = await domains_svc.create_domain(db, user, name=name)
    await db.flush()
    slug = domain.slug
    return RedirectResponse(f"/domains/{slug}", status_code=303)
