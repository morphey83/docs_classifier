"""Web UI: the dashboard (your domains) + create-domain."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import User
from app.services import documents as docs_svc
from app.services import domains as domains_svc
from app.web.csrf import CsrfGuard
from app.web.deps import current_user
from app.web.templating import render

router = APIRouter()


@router.get("/")
async def dashboard(
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    rows = await domains_svc.list_memberships(db, user)
    domains = []
    for domain, member in rows:
        domains.append(
            {
                "domain": domain,
                "role": member.role,
                "inbox": await docs_svc.inbox_count(db, domain.id),
            }
        )
    return render(request, "dashboard.html", {"domains": domains})


@router.post("/domains")
async def create_domain(
    request: Request,
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
