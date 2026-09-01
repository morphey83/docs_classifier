"""Document ingest (dedup / replace / new), CRUD, and the inbox queue."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import BinaryIO, Literal

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app import storage
from app.config import settings
from app.ingest import metadata
from app.models import (
    DocSource,
    DocStatus,
    Document,
    DocumentTag,
    DocumentVersion,
    Domain,
    InboxDefer,
    Tag,
    User,
)
from app.util.time import utcnow

Outcome = Literal["created", "deduplicated", "restored", "replaced", "new_from_conflict"]
OnConflict = Literal["replace", "new"]


class QuotaExceeded(Exception):
    def __init__(self, used: int, quota: int) -> None:
        super().__init__("domain storage quota exceeded")
        self.used = used
        self.quota = quota


class NameConflict(Exception):
    def __init__(self, existing_id: uuid.UUID) -> None:
        super().__init__("a different document with this name already exists")
        self.existing_id = existing_id


@dataclass
class IngestResult:
    document: Document
    outcome: Outcome


async def _domain_used_bytes(db: AsyncSession, domain_id: uuid.UUID) -> int:
    return int(
        await db.scalar(
            select(func.coalesce(func.sum(Document.size_bytes), 0)).where(
                Document.domain_id == domain_id, Document.deleted_at.is_(None)
            )
        )
        or 0
    )


def _quota_bytes(domain: Domain) -> int:
    mb = (domain.settings or {}).get("storage_quota_mb", settings.default_domain_quota_mb)
    return int(mb) * 1024 * 1024


async def _unique_title(db: AsyncSession, domain_id: uuid.UUID, base: str) -> str:
    taken = set(
        await db.scalars(
            select(Document.title).where(
                Document.domain_id == domain_id,
                Document.deleted_at.is_(None),
                Document.title.like(f"{base}%"),
            )
        )
    )
    if base not in taken:
        return base
    n = 2
    while f"{base} ({n})" in taken:
        n += 1
    return f"{base} ({n})"


async def ingest_upload(
    db: AsyncSession,
    domain: Domain,
    uploader: User,
    *,
    stream: BinaryIO,
    original_name: str,
    on_conflict: OnConflict | None = None,
    source: DocSource = DocSource.upload,
    batch_id: uuid.UUID | None = None,
) -> IngestResult:
    original_name = Path(original_name).name or "file"
    blob = await run_in_threadpool(storage.store_stream, stream)

    used = await _domain_used_bytes(db, domain.id)
    quota = _quota_bytes(domain)
    if used + blob.size > quota:
        raise QuotaExceeded(used, quota)

    # 1. exact content already here (not deleted) -> idempotent no-op
    dup = await db.scalar(
        select(Document).where(
            Document.domain_id == domain.id,
            Document.sha256 == blob.sha256,
            Document.deleted_at.is_(None),
        )
    )
    if dup is not None:
        return IngestResult(dup, "deduplicated")

    # 2. same content + name is sitting in the trash -> restore with its tags
    trashed = await db.scalar(
        select(Document).where(
            Document.domain_id == domain.id,
            Document.sha256 == blob.sha256,
            Document.original_name == original_name,
            Document.deleted_at.is_not(None),
        )
    )
    if trashed is not None:
        trashed.deleted_at = None
        trashed.deleted_by = None
        await db.flush()
        return IngestResult(trashed, "restored")

    meta = await run_in_threadpool(
        metadata.extract, storage.blob_path(blob.sha256), original_name
    )

    # 3. same name, different content -> conflict
    same_name = await db.scalar(
        select(Document).where(
            Document.domain_id == domain.id,
            Document.original_name == original_name,
            Document.deleted_at.is_(None),
        )
    )
    if same_name is not None:
        if on_conflict is None:
            raise NameConflict(same_name.id)
        if on_conflict == "replace":
            _snapshot_version(db, same_name)
            same_name.sha256 = blob.sha256
            same_name.storage_key = blob.storage_key
            same_name.size_bytes = blob.size
            same_name.mime = meta.mime
            same_name.ext = meta.ext
            if meta.doc_date is not None:
                same_name.doc_date = meta.doc_date
            same_name.version += 1
            same_name.uploaded_by = uploader.id
            same_name.uploaded_at = utcnow()
            await db.flush()
            return IngestResult(same_name, "replaced")
        # on_conflict == "new"
        title = await _unique_title(db, domain.id, Path(original_name).stem or original_name)
        doc = _new_document(domain, uploader, blob, meta, original_name, source, batch_id, title)
        db.add(doc)
        await db.flush()
        return IngestResult(doc, "new_from_conflict")

    # 4. brand new
    title = Path(original_name).stem or original_name
    doc = _new_document(domain, uploader, blob, meta, original_name, source, batch_id, title)
    db.add(doc)
    await db.flush()
    return IngestResult(doc, "created")


def _new_document(
    domain: Domain,
    uploader: User,
    blob: storage.BlobInfo,
    meta: metadata.FileMeta,
    original_name: str,
    source: DocSource,
    batch_id: uuid.UUID | None,
    title: str,
) -> Document:
    return Document(
        domain_id=domain.id,
        sha256=blob.sha256,
        storage_key=blob.storage_key,
        original_name=original_name,
        title=title,
        mime=meta.mime,
        ext=meta.ext,
        size_bytes=blob.size,
        doc_date=meta.doc_date,
        status=DocStatus.inbox,
        source=source,
        upload_batch_id=batch_id,
        uploaded_by=uploader.id,
    )


def _snapshot_version(db: AsyncSession, doc: Document) -> None:
    db.add(
        DocumentVersion(
            document_id=doc.id,
            version_no=doc.version,
            sha256=doc.sha256,
            storage_key=doc.storage_key,
            size_bytes=doc.size_bytes,
            doc_date=doc.doc_date,
            original_name=doc.original_name,
        )
    )


# --- CRUD / queries --------------------------------------------------------
async def get_document(db: AsyncSession, doc_id: uuid.UUID) -> Document | None:
    return await db.scalar(select(Document).where(Document.id == doc_id))


async def update_document(
    db: AsyncSession,
    doc: Document,
    *,
    title: str | None = None,
    doc_date: datetime | None = None,
    notes: str | None = None,
    clear_doc_date: bool = False,
) -> Document:
    if title is not None:
        doc.title = title.strip() or doc.title
    if clear_doc_date:
        doc.doc_date = None
    elif doc_date is not None:
        doc.doc_date = doc_date
    if notes is not None:
        doc.notes = notes or None
    await db.flush()
    return doc


async def list_documents(
    db: AsyncSession,
    domain_id: uuid.UUID,
    *,
    status: DocStatus | None = None,
    tag_slugs: list[str] | None = None,
    q: str | None = None,
    include_trash: bool = False,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[Document], int]:
    stmt = select(Document).where(Document.domain_id == domain_id)
    if not include_trash:
        stmt = stmt.where(Document.deleted_at.is_(None))
    if status is not None:
        stmt = stmt.where(Document.status == status)
    if q:
        stmt = stmt.where(Document.title.ilike(f"%{q}%"))
    if tag_slugs:
        for slug in tag_slugs:
            sub = (
                select(DocumentTag.document_id)
                .join(Tag, Tag.id == DocumentTag.tag_id)
                .where(Tag.domain_id == domain_id, Tag.slug == slug)
            )
            stmt = stmt.where(Document.id.in_(sub))

    total = int(
        await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    )
    rows = await db.scalars(
        stmt.order_by(Document.uploaded_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return list(rows), total


# --- inbox queue ---------------------------------------------------------
async def inbox_count(db: AsyncSession, domain_id: uuid.UUID) -> int:
    return int(
        await db.scalar(
            select(func.count()).select_from(Document).where(
                Document.domain_id == domain_id,
                Document.status == DocStatus.inbox,
                Document.deleted_at.is_(None),
            )
        )
        or 0
    )


async def next_inbox_document(
    db: AsyncSession, domain_id: uuid.UUID, user_id: uuid.UUID
) -> Document | None:
    deferred = select(InboxDefer.document_id).where(InboxDefer.user_id == user_id)
    return await db.scalar(
        select(Document)
        .where(
            Document.domain_id == domain_id,
            Document.status == DocStatus.inbox,
            Document.deleted_at.is_(None),
            Document.id.not_in(deferred),
        )
        .order_by(Document.uploaded_at.asc())
        .limit(1)
    )


async def complete_document(db: AsyncSession, doc: Document) -> None:
    doc.status = DocStatus.tagged
    await db.execute(delete(InboxDefer).where(InboxDefer.document_id == doc.id))
    await db.flush()


async def defer_document(db: AsyncSession, doc: Document, user_id: uuid.UUID) -> None:
    if await db.get(InboxDefer, (user_id, doc.id)) is None:
        db.add(InboxDefer(user_id=user_id, document_id=doc.id))
        await db.flush()


async def clear_defers(db: AsyncSession, domain_id: uuid.UUID, user_id: uuid.UUID) -> int:
    docs = select(Document.id).where(Document.domain_id == domain_id)
    result = await db.execute(
        delete(InboxDefer).where(
            InboxDefer.user_id == user_id, InboxDefer.document_id.in_(docs)
        )
    )
    return result.rowcount or 0
