"""Cross-domain document search and the tag-option picker (§7).

`GET /domains/{d}/documents` (app/api/documents.py) stays for domain-scoped
browsing; these routes are top-level — `domain_id` is an ordinary optional
filter, and when it's omitted the search spans every domain the caller
belongs to. Primarily for the bot, eventually a cross-domain web search too.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api._common import document_out
from app.db import get_session
from app.models import DocStatus, TextSource, User
from app.schemas.documents import DocumentList, Facets
from app.schemas.tags import TagOption
from app.security import get_current_user
from app.services import domains as domains_svc
from app.services import search as search_svc
from app.services import tags as tags_svc

router = APIRouter(tags=["search"])


async def _caller_domain_ids(
    db: AsyncSession, user: User, domain_id: uuid.UUID | None
) -> list[uuid.UUID]:
    memberships = await domains_svc.list_memberships(db, user)
    all_ids = [d.id for d, _ in memberships]
    if domain_id is None:
        return all_ids
    if domain_id not in all_ids:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "domain not found")
    return [domain_id]


def _csv(value: str | None) -> list[str]:
    return [s.strip() for s in value.split(",") if s.strip()] if value else []


@router.get("/documents", response_model=DocumentList)
async def search_documents_cross_domain(
    domain_id: uuid.UUID | None = Query(default=None),
    q: str | None = Query(default=None),
    status_: DocStatus | None = Query(default=None, alias="status"),
    tags: str | None = Query(default=None, description="tag names, comma-sep (all must match)"),
    tags_any: str | None = Query(default=None),
    tags_none: str | None = Query(default=None),
    ext: str | None = Query(default=None),
    mime: str | None = Query(default=None),
    size_min: int | None = Query(default=None, ge=0),
    size_max: int | None = Query(default=None, ge=0),
    doc_date_from: datetime | None = None,
    doc_date_to: datetime | None = None,
    uploaded_from: datetime | None = None,
    uploaded_to: datetime | None = None,
    uploaded_by: uuid.UUID | None = None,
    has_index: bool | None = None,
    has_ocr: bool | None = None,
    text_source: TextSource | None = None,
    sort: str = Query(default="uploaded_at"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    facets: bool = Query(default=True),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> DocumentList:
    domain_ids = await _caller_domain_ids(db, user, domain_id)
    f = search_svc.SearchFilters(
        q=q,
        status=status_,
        tags_all=_csv(tags),
        tags_any=_csv(tags_any),
        tags_none=_csv(tags_none),
        ext=ext,
        mime=mime,
        size_min=size_min,
        size_max=size_max,
        doc_date_from=doc_date_from,
        doc_date_to=doc_date_to,
        uploaded_from=uploaded_from,
        uploaded_to=uploaded_to,
        uploaded_by=uploaded_by,
        has_index=has_index,
        has_ocr=has_ocr,
        text_source=text_source,
        sort=sort,
        page=page,
        page_size=page_size,
    )
    docs, total, facet_data = await search_svc.search_documents(db, domain_ids, f)

    names = {}
    if docs:
        rows = await domains_svc.list_memberships(db, user)
        names = {d.id: d.name for d, _ in rows}
    items = []
    for d in docs:
        out = await document_out(db, d)
        out.domain_name = names.get(d.domain_id)
        items.append(out)

    return DocumentList(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        facets=Facets(**facet_data.__dict__) if facets else None,
    )


@router.get("/tags", response_model=list[TagOption])
async def list_tag_options(
    domain_id: uuid.UUID | None = Query(default=None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> list[TagOption]:
    domain_ids = await _caller_domain_ids(db, user, domain_id)
    return [
        TagOption(name=name, usage_count=count)
        for name, count in await tags_svc.suggest_tags(db, domain_ids, limit=None)
    ]
