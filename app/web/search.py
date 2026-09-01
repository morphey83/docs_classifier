"""Web UI: the faceted search page for one domain (full page + HTMX partial)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import DocStatus, DocumentTag, Tag
from app.services import search as search_svc
from app.web.deps import DomainView, domain_by_slug
from app.web.templating import render

router = APIRouter()

PAGE_SIZE = 25
_SORTS = {
    "uploaded_at": "по загрузке",
    "doc_date": "по дате документа",
    "title": "по названию",
    "size": "по размеру",
}


def _csv(value: str | None) -> list[str]:
    return [s.strip() for s in value.split(",") if s.strip()] if value else []


def _tri(value: str | None) -> bool | None:
    if value in ("yes", "true", "1"):
        return True
    if value in ("no", "false", "0"):
        return False
    return None


def _status(value: str | None) -> DocStatus | None:
    try:
        return DocStatus(value) if value else None
    except ValueError:
        return None


async def _tags_by_doc(db: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, list[str]]:
    if not ids:
        return {}
    rows = await db.execute(
        select(DocumentTag.document_id, Tag.name)
        .join(Tag, Tag.id == DocumentTag.tag_id)
        .where(DocumentTag.document_id.in_(ids))
    )
    out: dict[uuid.UUID, list[str]] = {}
    for did, name in rows:
        out.setdefault(did, []).append(name)
    return out


@router.get("/domains/{slug}/search")
async def search_page(
    request: Request,
    q: str | None = Query(default=None),
    tags: str | None = Query(default=None),
    type: str | None = Query(default=None),
    status: str | None = Query(default=None),
    has_ocr: str | None = Query(default=None),
    has_index: str | None = Query(default=None),
    sort: str = Query(default="uploaded_at"),
    page: int = Query(default=1, ge=1),
    view: DomainView = Depends(domain_by_slug),
    db: AsyncSession = Depends(get_session),
) -> Response:
    status_enum = _status(status)
    f = search_svc.SearchFilters(
        q=q or None,
        tags_all=_csv(tags),
        ext=type or None,
        status=status_enum,
        has_ocr=_tri(has_ocr),
        has_index=_tri(has_index),
        sort=sort if sort in _SORTS else "uploaded_at",
        page=page,
        page_size=PAGE_SIZE,
    )
    docs, total, facets = await search_svc.search_documents(db, [view.domain.id], f)
    tag_map = await _tags_by_doc(db, [d.id for d in docs])
    pages = max(1, -(-total // PAGE_SIZE))

    ctx = {
        "view": view,
        "partial": "_results.html",
        "docs": docs,
        "tag_map": tag_map,
        "total": total,
        "page": page,
        "pages": pages,
        "facets": facets,
        "sorts": _SORTS,
        "f": {
            "q": q or "",
            "tags": tags or "",
            "type": type or "",
            "status": status_enum.value if status_enum else "",
            "has_ocr": has_ocr or "",
            "has_index": has_index or "",
            "sort": f.sort,
        },
    }
    return render(request, "search.html", ctx)
