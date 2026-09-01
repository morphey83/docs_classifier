"""Web UI: a single domain's overview page."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.services import documents as docs_svc
from app.services import search as search_svc
from app.services import tags as tags_svc
from app.web.deps import DomainView, domain_by_slug
from app.web.templating import render

router = APIRouter()


@router.get("/domains/{slug}")
async def domain_overview(
    request: Request,
    view: DomainView = Depends(domain_by_slug),
    db: AsyncSession = Depends(get_session),
) -> Response:
    _docs, total, _facets = await search_svc.search_documents(
        db, [view.domain.id], search_svc.SearchFilters(page=1, page_size=1)
    )
    tags = await tags_svc.list_tags(db, view.domain.id)
    return render(
        request,
        "domain.html",
        {
            "view": view,
            "doc_total": total,
            "inbox": await docs_svc.inbox_count(db, view.domain.id),
            "tag_count": len(tags),
        },
    )
