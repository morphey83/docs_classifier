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
    DocumentVersion,
    Domain,
    InboxDefer,
    IndexStatus,
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


class DisallowedType(Exception):
    def __init__(self, ext: str, mime: str, allowed: set[str]) -> None:
        super().__init__(f"file type '{ext or mime}' is not allowed in this domain")
        self.ext = ext
        self.mime = mime
        self.allowed = sorted(allowed)


def effective_allowed_types(domain: Domain) -> set[str] | None:
    """The domain's file-type allowlist (extensions, lowercased) or ``None`` = unrestricted.

    An instance-wide master list (``ALLOWED_TYPES_MASTER``), if set, is the hard
    ceiling — a domain can only narrow it, never add types outside it.
    """
    master = settings.allowed_types_master_set
    val = (domain.settings or {}).get("allowed_types")
    if val is None:
        chosen = settings.default_allowed_types_set
    else:
        chosen = {str(v).strip().lower().lstrip(".") for v in val if str(v).strip()}
    if master is None:
        return chosen
    return master if chosen is None else (chosen & master)


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


def default_is_public(domain: Domain) -> bool:
    """The visibility a new document in this domain gets (§15)."""
    return (domain.settings or {}).get("default_document_visibility") == "public"


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


async def store_and_probe(
    stream: BinaryIO, original_name: str
) -> tuple[storage.BlobInfo, metadata.FileMeta]:
    """Persist an uploaded stream to the blob store and read its file metadata."""
    blob = await run_in_threadpool(storage.store_stream, stream)
    async with storage.fetch_local(storage.blobs_store(), blob.storage_key) as path:
        meta = await run_in_threadpool(metadata.extract, path, original_name)
    return blob, meta


async def ingest_upload(
    db: AsyncSession,
    domain: Domain,
    uploader: User,
    *,
    stream: BinaryIO | None = None,
    original_name: str,
    blob: storage.BlobInfo | None = None,
    meta: metadata.FileMeta | None = None,
    on_conflict: OnConflict | None = None,
    source: DocSource = DocSource.upload,
    batch_id: uuid.UUID | None = None,
) -> IngestResult:
    original_name = original_name.strip() or "file"
    if blob is None or meta is None:
        assert stream is not None
        blob, meta = await store_and_probe(stream, original_name)

    allowed = effective_allowed_types(domain)
    if allowed is not None and meta.ext.lower().lstrip(".") not in allowed:
        raise DisallowedType(meta.ext, meta.mime, allowed)

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
        is_public=default_is_public(domain),
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
    retitled = False
    if title is not None:
        new_title = title.strip() or doc.title
        retitled = new_title != doc.title
        doc.title = new_title
    if clear_doc_date:
        doc.doc_date = None
    elif doc_date is not None:
        doc.doc_date = doc_date
    if notes is not None:
        doc.notes = notes or None
    await db.flush()
    if retitled and doc.index_status == IndexStatus.done:
        # search_tsv is built from title + extracted_text and isn't otherwise
        # refreshed on an edit — keep it in sync silently (docs/architecture.md §8).
        from app.services.search import index_document

        await index_document(db, doc, reparse=False)
    return doc


# --- inbox queue ---------------------------------------------------------
# The count means "documents still waiting for *this* user to tag them", so it
# lines up with what the /search inbox preset shows: untagged, not deferred by
# them. Pass ``user_id=None`` for a plain "untagged" count.
async def inbox_count(
    db: AsyncSession, domain_id: uuid.UUID, user_id: uuid.UUID | None = None
) -> int:
    return await inbox_count_across(db, [domain_id], user_id)


async def inbox_count_across(db: AsyncSession, domain_ids, user_id: uuid.UUID | None = None) -> int:
    if not domain_ids:
        return 0
    stmt = (
        select(func.count())
        .select_from(Document)
        .where(
            Document.domain_id.in_(list(domain_ids)),
            Document.status == DocStatus.inbox,
            Document.deleted_at.is_(None),
        )
    )
    if user_id is not None:
        stmt = stmt.where(
            Document.id.not_in(
                select(InboxDefer.document_id).where(InboxDefer.user_id == user_id)
            )
        )
    return int(await db.scalar(stmt) or 0)


async def next_inbox_across(
    db: AsyncSession, domain_ids, user_id: uuid.UUID
) -> Document | None:
    if not domain_ids:
        return None
    deferred = select(InboxDefer.document_id).where(InboxDefer.user_id == user_id)
    return await db.scalar(
        select(Document)
        .where(
            Document.domain_id.in_(list(domain_ids)),
            Document.status == DocStatus.inbox,
            Document.deleted_at.is_(None),
            Document.id.not_in(deferred),
        )
        .order_by(Document.uploaded_at.asc())
        .limit(1)
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
    # a document leaves the inbox by gaining a tag (set_document_tags keeps
    # Document.status in step) — here we only clear this user's "defer".
    await db.execute(delete(InboxDefer).where(InboxDefer.document_id == doc.id))
    await db.flush()


async def defer_document(db: AsyncSession, doc: Document, user_id: uuid.UUID) -> None:
    if await db.get(InboxDefer, (user_id, doc.id)) is None:
        db.add(InboxDefer(user_id=user_id, document_id=doc.id))
        await db.flush()


async def clear_defers(db: AsyncSession, domain_id: uuid.UUID, user_id: uuid.UUID) -> int:
    docs = select(Document.id).where(Document.domain_id == domain_id)
    result = await db.execute(
        delete(InboxDefer).where(InboxDefer.user_id == user_id, InboxDefer.document_id.in_(docs))
    )
    return result.rowcount or 0
