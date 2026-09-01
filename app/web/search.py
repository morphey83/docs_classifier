"""Web UI: the one document search — root-level, with a domain filter,
card / table views, and column sorting (full page + HTMX partial)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import DocStatus, Document, DocumentTag, Tag, User
from app.services import domains as domains_svc
from app.services import search as search_svc
from app.web.deps import current_user
from app.web.templating import render

router = APIRouter()

PAGE_SIZE = 25
SORTS = {
    "uploaded_at": "загружен",
    "doc_date": "дата документа",
    "title": "название",
    "size": "размер",
    "status": "статус",
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


async def _distinct_exts(db: AsyncSession, domain_ids: list[uuid.UUID]) -> list[str]:
    """Extensions actually present in the caller's documents — feeds the filter."""
    if not domain_ids:
        return []
    rows = await db.scalars(
        select(Document.ext)
        .where(
            Document.domain_id.in_(domain_ids),
            Document.deleted_at.is_(None),
            Document.ext.is_not(None),
            Document.ext != "",
        )
        .distinct()
    )
    return sorted({e.lower() for e in rows})


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


@router.get("/search")
async def search(
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    p = request.query_params
    memberships = await domains_svc.list_memberships(db, user)
    dom_by_id = {d.id: d for d, _ in memberships}

    raw_domain = p.get("domain_id") or ""
    scope_ids = list(dom_by_id)
    try:
        picked = uuid.UUID(raw_domain) if raw_domain else None
    except ValueError:
        picked = None
    if picked is not None and picked in dom_by_id:
        scope_ids = [picked]
    else:
        picked = None

    sort = p.get("sort") if p.get("sort") in SORTS else "uploaded_at"
    sort_dir = "asc" if p.get("dir") == "asc" else "desc"
    view = "table" if p.get("view") == "table" else "cards"
    status_enum = _status(p.get("status"))

    f = search_svc.SearchFilters(
        q=(p.get("q") or "") or None,
        tags_all=_csv(p.get("tags")),
        ext=(p.get("type") or "") or None,
        status=status_enum,
        has_ocr=_tri(p.get("has_ocr")),
        has_index=_tri(p.get("has_index")),
        sort=sort,
        sort_dir=sort_dir,
        page=max(1, int(p.get("page") or 1)),
        page_size=PAGE_SIZE,
    )
    docs, total, _facets = await search_svc.search_documents(db, scope_ids, f)

    ctx = {
        "partial": "_results.html",
        "docs": docs,
        "tag_map": await _tags_by_doc(db, [d.id for d in docs]),
        "domain_names": {d.id: d.name for d, _ in memberships},
        "domain_slugs": {d.id: d.slug for d, _ in memberships},
        "total": total,
        "page": f.page,
        "pages": max(1, -(-total // PAGE_SIZE)),
        "ext_options": await _distinct_exts(db, list(dom_by_id)),
        "sorts": SORTS,
        "view": view,
        "domains": [d for d, _ in memberships],
        "f": {
            "q": p.get("q") or "",
            "tags": p.get("tags") or "",
            "type": p.get("type") or "",
            "status": status_enum.value if status_enum else "",
            "has_ocr": p.get("has_ocr") or "",
            "has_index": p.get("has_index") or "",
            "domain_id": str(picked) if picked else "",
            "sort": sort,
            "dir": sort_dir,
        },
    }
    return render(request, "search.html", ctx)
