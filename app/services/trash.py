"""Soft delete, restore, and hard purge with blob refcount GC (§14)."""

from __future__ import annotations

import uuid

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.models import (
    Document,
    DocumentSetItem,
    DocumentTag,
    DocumentVersion,
    InboxDefer,
    UploadBatchItem,
    User,
)
from app.util.time import utcnow


class TrashError(ValueError):
    pass


async def soft_delete(db: AsyncSession, doc: Document, actor: User) -> Document:
    if doc.deleted_at is None:
        doc.deleted_at = utcnow()
        doc.deleted_by = actor.id
        await db.flush()
    return doc


async def restore(db: AsyncSession, doc: Document) -> Document:
    if doc.deleted_at is None:
        return doc
    clash = await db.scalar(
        select(Document.id).where(
            Document.domain_id == doc.domain_id,
            Document.sha256 == doc.sha256,
            Document.deleted_at.is_(None),
            Document.id != doc.id,
        )
    )
    if clash is not None:
        raise TrashError("an active document with the same content already exists")
    doc.deleted_at = None
    doc.deleted_by = None
    await db.flush()
    return doc


async def _blob_referenced(db: AsyncSession, sha256: str) -> bool:
    if await db.scalar(select(Document.id).where(Document.sha256 == sha256).limit(1)):
        return True
    return bool(
        await db.scalar(
            select(DocumentVersion.id).where(DocumentVersion.sha256 == sha256).limit(1)
        )
    )


async def hard_purge(db: AsyncSession, doc: Document) -> None:
    """Delete the document + its child rows, then GC any now-unreferenced blobs.

    Child rows are removed explicitly rather than via ``ON DELETE`` so the
    behaviour is identical on SQLite (tests) and PostgreSQL.
    """
    doc_id = doc.id
    hashes = {doc.sha256}
    hashes.update(
        await db.scalars(
            select(DocumentVersion.sha256).where(DocumentVersion.document_id == doc_id)
        )
    )
    await db.execute(delete(DocumentTag).where(DocumentTag.document_id == doc_id))
    await db.execute(delete(DocumentVersion).where(DocumentVersion.document_id == doc_id))
    await db.execute(delete(DocumentSetItem).where(DocumentSetItem.document_id == doc_id))
    await db.execute(delete(InboxDefer).where(InboxDefer.document_id == doc_id))
    await db.execute(
        update(UploadBatchItem)
        .where(UploadBatchItem.document_id == doc_id)
        .values(document_id=None)
    )
    await db.delete(doc)
    await db.flush()
    for h in hashes:
        if not await _blob_referenced(db, h):
            storage.delete_blob(h)
            storage.remove_derived(h)


async def list_trash(
    db: AsyncSession, domain_id: uuid.UUID, *, page: int = 1, page_size: int = 50
) -> tuple[list[Document], int]:
    base = select(Document).where(
        Document.domain_id == domain_id, Document.deleted_at.is_not(None)
    )
    total = int(await db.scalar(select(func.count()).select_from(base.subquery())) or 0)
    docs = list(
        await db.scalars(
            base.order_by(Document.deleted_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    return docs, total


async def purge_domain_trash(db: AsyncSession, domain_id: uuid.UUID) -> int:
    docs = list(
        await db.scalars(
            select(Document).where(
                Document.domain_id == domain_id, Document.deleted_at.is_not(None)
            )
        )
    )
    for doc in docs:
        await hard_purge(db, doc)
    return len(docs)
