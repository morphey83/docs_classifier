"""Body-text indexing and faceted document search.

PostgreSQL: `to_tsvector('russian', …)` for the body + `pg_trgm`-backed
`ILIKE` on the title. SQLite (tests): plain `LIKE` on title + extracted_text.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import Select, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app import storage
from app.ingest import text as text_extract
from app.models import (
    DocStatus,
    Document,
    DocumentTag,
    IndexStatus,
    Tag,
    TextSource,
)
from app.util.slug import slugify
from app.util.time import utcnow

FTS_CONFIG = "russian"


def _is_pg(db: AsyncSession) -> bool:
    return db.bind.dialect.name == "postgresql"


async def index_document(db: AsyncSession, doc: Document, *, reparse: bool = False) -> Document:
    if doc.text_source == TextSource.none or reparse:
        try:
            async with storage.fetch_local(
                storage.blobs_store(), storage.blob_key(doc.sha256)
            ) as blob:
                body = await run_in_threadpool(
                    text_extract.extract_text, blob, doc.mime, doc.ext
                )
        except storage.ObjectNotFound:
            body = None
        doc.extracted_text = body or None
        doc.text_source = TextSource.parsed if body else TextSource.none

    if _is_pg(db):
        await db.execute(
            text(
                "UPDATE document SET search_tsv = "
                "to_tsvector(:cfg, coalesce(title,'') || ' ' || coalesce(extracted_text,'')) "
                "WHERE id = :id"
            ),
            {"cfg": FTS_CONFIG, "id": str(doc.id)},
        )

    doc.index_status = IndexStatus.done
    doc.indexed_at = utcnow()
    await db.flush()
    return doc


# --- search --------------------------------------------------------------
@dataclass
class SearchFilters:
    q: str | None = None
    status: DocStatus | None = None
    tags_all: list[str] = field(default_factory=list)
    tags_any: list[str] = field(default_factory=list)
    tags_none: list[str] = field(default_factory=list)
    ext: str | None = None
    mime: str | None = None
    size_min: int | None = None
    size_max: int | None = None
    doc_date_from: datetime | None = None
    doc_date_to: datetime | None = None
    uploaded_from: datetime | None = None
    uploaded_to: datetime | None = None
    uploaded_by: uuid.UUID | None = None
    has_index: bool | None = None
    indexed_from: datetime | None = None
    indexed_to: datetime | None = None
    has_ocr: bool | None = None
    ocr_from: datetime | None = None
    ocr_to: datetime | None = None
    text_source: TextSource | None = None
    include_trash: bool = False  # deleted + active
    only_trash: bool = False  # deleted only (the "Корзина" search preset, §14)
    exclude_deferred_by: uuid.UUID | None = None  # drop docs this user deferred (inbox preset)
    # Domain scope — only meaningful for a saved set filter (§15); empty means
    # "every domain the owner can currently reach". The live /search UI passes
    # its scope to search_documents() directly and leaves this empty.
    domain_ids: list[uuid.UUID] = field(default_factory=list)
    sort: str = "uploaded_at"
    sort_dir: str = "desc"  # "asc" | "desc"
    page: int = 1
    page_size: int = 50

    # --- serialization for document_set_filter.filter (JSON) --------------
    _SCALARS = (
        "q", "ext", "mime", "size_min", "size_max", "uploaded_by",
        "has_index", "has_ocr", "include_trash", "sort", "sort_dir",
    )
    _DATES = (
        "doc_date_from", "doc_date_to", "uploaded_from", "uploaded_to",
        "indexed_from", "indexed_to", "ocr_from", "ocr_to",
    )

    def to_dict(self) -> dict:
        out: dict = {}
        for k in self._SCALARS:
            v = getattr(self, k)
            if v not in (None, "", False):
                out[k] = str(v) if isinstance(v, uuid.UUID) else v
        for k in self._DATES:
            v = getattr(self, k)
            if v is not None:
                out[k] = v.isoformat()
        for k in ("tags_all", "tags_any", "tags_none"):
            if getattr(self, k):
                out[k] = list(getattr(self, k))
        if self.status is not None:
            out["status"] = self.status.value
        if self.text_source is not None:
            out["text_source"] = self.text_source.value
        if self.domain_ids:
            out["domain_ids"] = [str(d) for d in self.domain_ids]
        return out

    @classmethod
    def from_dict(cls, data: Mapping | None) -> SearchFilters:
        data = data or {}
        f = cls()
        for k in cls._SCALARS:
            if k in data:
                setattr(f, k, data[k])
        if isinstance(f.uploaded_by, str):
            with contextlib.suppress(ValueError):
                f.uploaded_by = uuid.UUID(f.uploaded_by)
        if not isinstance(f.uploaded_by, uuid.UUID):
            f.uploaded_by = None
        for k in cls._DATES:
            if data.get(k):
                with contextlib.suppress(TypeError, ValueError):
                    setattr(f, k, datetime.fromisoformat(data[k]))
        for k in ("tags_all", "tags_any", "tags_none"):
            if data.get(k):
                setattr(f, k, [str(t) for t in data[k]])
        if data.get("status"):
            with contextlib.suppress(ValueError):
                f.status = DocStatus(data["status"])
        if data.get("text_source"):
            with contextlib.suppress(ValueError):
                f.text_source = TextSource(data["text_source"])
        f.domain_ids = []
        for d in data.get("domain_ids", []) or []:
            with contextlib.suppress(ValueError):
                f.domain_ids.append(uuid.UUID(str(d)))
        return f


@dataclass
class Facets:
    tags: list[dict]
    types: list[dict]
    status: dict[str, int]


TagIndex = dict[str, list[uuid.UUID]]


async def _tag_slug_index(db: AsyncSession) -> TagIndex:
    """``{tag slug: [tag id]}`` — tags are global and slug-unique (§7), so a
    caller's free-text name resolves through :func:`slugify` to at most one id.
    Built once per search."""
    idx: TagIndex = {}
    for tid, slug in await db.execute(select(Tag.id, Tag.slug)):
        idx.setdefault(slug, []).append(tid)
    return idx


def _tag_doc_subquery(tag_ids: list[uuid.UUID]) -> Select:
    return select(DocumentTag.document_id).where(DocumentTag.tag_id.in_(tag_ids))


_STATUS_RU = {
    DocStatus.inbox: "в очереди",
    DocStatus.tagged: "размечен",
}


def describe_filters(f: SearchFilters, domain_names: Mapping[uuid.UUID, str] | None = None) -> str:
    """A short human summary of a saved set filter, for the set card."""
    parts: list[str] = []
    if f.domain_ids and domain_names:
        parts.append(" / ".join(domain_names.get(d, "?") for d in f.domain_ids))
    if f.q:
        parts.append(f'«{f.q}»')
    parts += [f"#{t}" for t in f.tags_all]
    parts += [f"~{t}" for t in f.tags_any]
    parts += [f"-{t}" for t in f.tags_none]
    if f.ext:
        parts.append(f.ext.lower().lstrip("."))
    if f.status is not None:
        parts.append(_STATUS_RU.get(f.status, str(f.status)))
    if f.has_ocr is True:
        parts.append("OCR")
    if f.has_index is True:
        parts.append("проиндексирован")
    for lo, hi, label in (
        (f.doc_date_from, f.doc_date_to, "дата док."),
        (f.uploaded_from, f.uploaded_to, "загружен"),
    ):
        if lo or hi:
            span = f"{lo:%Y-%m-%d}" if lo else "..."
            span += f" - {hi:%Y-%m-%d}" if hi else " - ..."
            parts.append(f"{label} {span}")
    return " · ".join(parts) or "все документы"


def _apply(
    stmt: Select,
    domain_ids: Sequence[uuid.UUID],
    f: SearchFilters,
    *,
    pg: bool,
    tag_index: TagIndex,
) -> Select:
    stmt = stmt.where(Document.domain_id.in_(domain_ids))
    if f.only_trash:
        stmt = stmt.where(Document.deleted_at.is_not(None))
    elif not f.include_trash:
        stmt = stmt.where(Document.deleted_at.is_(None))
    if f.exclude_deferred_by is not None:
        from app.models import InboxDefer

        stmt = stmt.where(
            Document.id.not_in(
                select(InboxDefer.document_id).where(
                    InboxDefer.user_id == f.exclude_deferred_by
                )
            )
        )
    if f.status is not None:
        stmt = stmt.where(Document.status == f.status)
    if f.ext:
        stmt = stmt.where(Document.ext == f.ext.lower().lstrip("."))
    if f.mime:
        stmt = stmt.where(Document.mime == f.mime)
    if f.size_min is not None:
        stmt = stmt.where(Document.size_bytes >= f.size_min)
    if f.size_max is not None:
        stmt = stmt.where(Document.size_bytes <= f.size_max)
    if f.doc_date_from is not None:
        stmt = stmt.where(Document.doc_date >= f.doc_date_from)
    if f.doc_date_to is not None:
        stmt = stmt.where(Document.doc_date <= f.doc_date_to)
    if f.uploaded_from is not None:
        stmt = stmt.where(Document.uploaded_at >= f.uploaded_from)
    if f.uploaded_to is not None:
        stmt = stmt.where(Document.uploaded_at <= f.uploaded_to)
    if f.uploaded_by is not None:
        stmt = stmt.where(Document.uploaded_by == f.uploaded_by)
    if f.has_index is True:
        stmt = stmt.where(Document.indexed_at.is_not(None))
    if f.has_index is False:
        stmt = stmt.where(Document.indexed_at.is_(None))
    if f.indexed_from is not None:
        stmt = stmt.where(Document.indexed_at >= f.indexed_from)
    if f.indexed_to is not None:
        stmt = stmt.where(Document.indexed_at <= f.indexed_to)
    if f.has_ocr is True:
        stmt = stmt.where(Document.ocr_at.is_not(None))
    if f.has_ocr is False:
        stmt = stmt.where(Document.ocr_at.is_(None))
    if f.ocr_from is not None:
        stmt = stmt.where(Document.ocr_at >= f.ocr_from)
    if f.ocr_to is not None:
        stmt = stmt.where(Document.ocr_at <= f.ocr_to)
    if f.text_source is not None:
        stmt = stmt.where(Document.text_source == f.text_source)

    def _ids(name: str) -> list[uuid.UUID]:
        return tag_index.get(slugify(name), [])

    for name in f.tags_all:
        stmt = stmt.where(Document.id.in_(_tag_doc_subquery(_ids(name))))
    if f.tags_any:
        subs = [_tag_doc_subquery(_ids(n)) for n in f.tags_any]
        stmt = stmt.where(or_(*[Document.id.in_(s) for s in subs]))
    for name in f.tags_none:
        stmt = stmt.where(Document.id.not_in(_tag_doc_subquery(_ids(name))))

    if f.q:
        like = f"%{f.q}%"
        if pg:
            stmt = stmt.where(
                or_(
                    text("document.search_tsv @@ websearch_to_tsquery(:cfg, :q)").bindparams(
                        cfg=FTS_CONFIG, q=f.q
                    ),
                    Document.title.ilike(like),
                )
            )
        else:
            stmt = stmt.where(or_(Document.title.ilike(like), Document.extracted_text.ilike(like)))
    return stmt


_SORT_COLS = {
    "uploaded_at": Document.uploaded_at,
    "doc_date": Document.doc_date,
    "size": Document.size_bytes,
    "title": Document.title,
    "status": Document.status,
    "indexed_at": Document.indexed_at,
    "ocr_at": Document.ocr_at,
}


def _order_by(f: SearchFilters):
    col = _SORT_COLS.get(f.sort, Document.uploaded_at)
    clause = col.asc() if f.sort_dir == "asc" else col.desc()
    return clause.nulls_last() if hasattr(clause, "nulls_last") else clause


async def search_documents(
    db: AsyncSession, domain_ids: Sequence[uuid.UUID], f: SearchFilters
) -> tuple[list[Document], int, Facets]:
    if not domain_ids:
        return [], 0, Facets(tags=[], types=[], status={})

    pg = _is_pg(db)
    needs_tags = bool(f.tags_all or f.tags_any or f.tags_none)
    tag_index = await _tag_slug_index(db) if needs_tags else {}
    base = _apply(select(Document), domain_ids, f, pg=pg, tag_index=tag_index)

    total = int(await db.scalar(select(func.count()).select_from(base.subquery())) or 0)

    order = _order_by(f)
    if f.sort == "relevance" and pg and f.q:
        order = text(
            "ts_rank_cd(document.search_tsv, websearch_to_tsquery(:cfg, :q)) DESC"
        ).bindparams(cfg=FTS_CONFIG, q=f.q)
    rows = await db.scalars(
        base.order_by(order).offset((f.page - 1) * f.page_size).limit(f.page_size)
    )
    docs = list(rows)

    id_sub = _apply(select(Document.id), domain_ids, f, pg=pg, tag_index=tag_index).subquery()

    tag_rows = await db.execute(
        select(Tag.slug, Tag.name, func.count(DocumentTag.document_id))
        .join(DocumentTag, DocumentTag.tag_id == Tag.id)
        .where(DocumentTag.document_id.in_(select(id_sub.c.id)))
        .group_by(Tag.slug, Tag.name)
        .order_by(func.count(DocumentTag.document_id).desc())
    )
    type_rows = await db.execute(
        select(Document.mime, func.count())
        .where(Document.id.in_(select(id_sub.c.id)))
        .group_by(Document.mime)
        .order_by(func.count().desc())
    )
    status_rows = await db.execute(
        select(Document.status, func.count())
        .where(Document.id.in_(select(id_sub.c.id)))
        .group_by(Document.status)
    )
    facets = Facets(
        tags=[{"slug": s, "name": n, "count": c} for s, n, c in tag_rows],
        types=[{"mime": m, "count": c} for m, c in type_rows],
        status={str(s): c for s, c in status_rows},
    )
    return docs, total, facets
