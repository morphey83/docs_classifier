"""Web UI: the faceted search page for one domain (full page + HTMX partial)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import DocStatus, DocumentTag, Tag, User
from app.services import domains as domains_svc
from app.services import search as search_svc
from app.web.deps import DomainView, current_user, domain_by_slug
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


def _parse(params) -> tuple[search_svc.SearchFilters, dict]:
    status_enum = _status(params.get("status"))
    sort = params.get("sort") or "uploaded_at"
    f = search_svc.SearchFilters(
        q=(params.get("q") or "") or None,
        tags_all=_csv(params.get("tags")),
        ext=(params.get("type") or "") or None,
        status=status_enum,
        has_ocr=_tri(params.get("has_ocr")),
        has_index=_tri(params.get("has_index")),
        sort=sort if sort in _SORTS else "uploaded_at",
        page=max(1, int(params.get("page") or 1)),
        page_size=PAGE_SIZE,
    )
    raw = {
        "q": params.get("q") or "",
        "tags": params.get("tags") or "",
        "type": params.get("type") or "",
        "status": status_enum.value if status_enum else "",
        "has_ocr": params.get("has_ocr") or "",
        "has_index": params.get("has_index") or "",
        "sort": f.sort,
    }
    return f, raw


async def _ctx(db: AsyncSession, f, raw, docs, total, facets) -> dict:
    return {
        "partial": "_results.html",
        "docs": docs,
        "tag_map": await _tags_by_doc(db, [d.id for d in docs]),
        "total": total,
        "page": f.page,
        "pages": max(1, -(-total // PAGE_SIZE)),
        "facets": facets,
        "sorts": _SORTS,
        "f": raw,
    }


@router.get("/domains/{slug}/search")
async def search_page(
    request: Request,
    view: DomainView = Depends(domain_by_slug),
    db: AsyncSession = Depends(get_session),
) -> Response:
    f, raw = _parse(request.query_params)
    docs, total, facets = await search_svc.search_documents(db, [view.domain.id], f)
    ctx = await _ctx(db, f, raw, docs, total, facets)
    ctx["view"] = view
    return render(request, "search.html", ctx)


@router.get("/search")
async def global_search(
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    f, raw = _parse(request.query_params)
    rows = await domains_svc.list_memberships(db, user)
    docs, total, facets = await search_svc.search_documents(db, [d.id for d, _ in rows], f)
    ctx = await _ctx(db, f, raw, docs, total, facets)
    ctx["domain_names"] = {d.id: d.name for d, _ in rows}
    return render(request, "search_global.html", ctx)
