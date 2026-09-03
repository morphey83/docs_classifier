"""Web UI: a single domain's overview — opens in the shared detail modal."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.rbac import Cap
from app.services import documents as docs_svc
from app.services import search as search_svc
from app.web.csrf import CsrfGuard
from app.web.deps import DomainView, domain_by_slug, require_cap
from app.web.templating import render

router = APIRouter()


async def _overview_ctx(db: AsyncSession, view: DomainView) -> dict:
    _docs, total, _facets = await search_svc.search_documents(
        db, [view.domain.id], search_svc.SearchFilters(page=1, page_size=1)
    )
    return {
        "partial": "_domain_body.html",
        "view": view,
        "doc_total": total,
        "inbox": await docs_svc.inbox_count(db, view.domain.id),
    }


@router.get("/domains/{slug}")
async def domain_overview(
    request: Request,
    view: DomainView = Depends(domain_by_slug),
    db: AsyncSession = Depends(get_session),
) -> Response:
    return render(request, "domain.html", await _overview_ctx(db, view))


@router.post("/domains/{slug}/rename")
async def domain_rename(
    request: Request,
    name: str = Form(...),
    _: None = CsrfGuard,
    view: DomainView = Depends(domain_by_slug),
    db: AsyncSession = Depends(get_session),
) -> Response:
    require_cap(view, Cap.manage)
    name = name.strip()
    if name:
        view.domain.name = name
        await db.flush()
    if request.headers.get("HX-Request"):
        return render(request, "domain.html", await _overview_ctx(db, view), toast="Сохранено")
    return RedirectResponse(f"/domains/{view.domain.slug}", status_code=303)
