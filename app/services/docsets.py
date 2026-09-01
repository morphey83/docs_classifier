"""Document sets, the set-archive content hash, and the (re)build job (§15)."""

from __future__ import annotations

import hashlib
import json
import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app import storage
from app.db import get_sessionmaker
from app.models import (
    Artifact,
    ArtifactKind,
    ArtifactStatus,
    Document,
    DocumentSet,
    DocumentSetItem,
    DocumentTag,
    Domain,
    SetVisibility,
    Tag,
    User,
)
from app.services.export import write_document_zip


class SetError(ValueError):
    pass


# --- CRUD ---------------------------------------------------------------
async def create_set(
    db: AsyncSession,
    domain: Domain,
    user: User,
    *,
    name: str,
    description: str | None,
    visibility: SetVisibility,
    document_ids: list[uuid.UUID] | None = None,
) -> DocumentSet:
    s = DocumentSet(
        domain_id=domain.id,
        name=name.strip(),
        description=(description or None),
        visibility=visibility,
        created_by=user.id,
    )
    db.add(s)
    await db.flush()
    if document_ids:
        await add_items(db, s, document_ids, actor=user)
    else:
        await _refresh_item_count(db, s)
    return s


async def list_sets(
    db: AsyncSession, domain_id: uuid.UUID, user_id: uuid.UUID
) -> list[DocumentSet]:
    rows = await db.scalars(
        select(DocumentSet)
        .where(
            DocumentSet.domain_id == domain_id,
            or_(
                DocumentSet.visibility == SetVisibility.domain,
                DocumentSet.created_by == user_id,
            ),
        )
        .order_by(DocumentSet.updated_at.desc())
    )
    return list(rows)


async def add_items(
    db: AsyncSession, set_obj: DocumentSet, doc_ids: list[uuid.UUID], *, actor: User
) -> int:
    valid = set(
        await db.scalars(
            select(Document.id).where(
                Document.domain_id == set_obj.domain_id,
                Document.id.in_(doc_ids),
                Document.deleted_at.is_(None),
            )
        )
    )
    present = set(
        await db.scalars(
            select(DocumentSetItem.document_id).where(DocumentSetItem.set_id == set_obj.id)
        )
    )
    pos = int(
        await db.scalar(
            select(func.coalesce(func.max(DocumentSetItem.position), 0)).where(
                DocumentSetItem.set_id == set_obj.id
            )
        )
        or 0
    )
    added = 0
    for did in doc_ids:
        if did not in valid or did in present:
            continue
        pos += 1
        db.add(
            DocumentSetItem(
                set_id=set_obj.id, document_id=did, added_by=actor.id, position=pos
            )
        )
        present.add(did)
        added += 1
    await db.flush()
    await _refresh_item_count(db, set_obj)
    return added


async def remove_item(
    db: AsyncSession, set_obj: DocumentSet, document_id: uuid.UUID
) -> bool:
    item = await db.get(DocumentSetItem, (set_obj.id, document_id))
    if item is None:
        return False
    await db.delete(item)
    await db.flush()
    await _refresh_item_count(db, set_obj)
    return True


async def _refresh_item_count(db: AsyncSession, set_obj: DocumentSet) -> None:
    set_obj.item_count = int(
        await db.scalar(
            select(func.count()).where(DocumentSetItem.set_id == set_obj.id)
        )
        or 0
    )
    await db.flush()


# --- set content, hashing, artifact ------------------------------------
async def set_documents(db: AsyncSession, set_id: uuid.UUID) -> list[Document]:
    """Non-deleted documents of a set, in the set's order."""
    rows = await db.scalars(
        select(Document)
        .join(DocumentSetItem, DocumentSetItem.document_id == Document.id)
        .where(DocumentSetItem.set_id == set_id, Document.deleted_at.is_(None))
        .order_by(DocumentSetItem.position, DocumentSetItem.added_at)
    )
    return list(rows)


async def tags_by_doc(
    db: AsyncSession, doc_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[tuple[str, str]]]:
    if not doc_ids:
        return {}
    rows = await db.execute(
        select(DocumentTag.document_id, Tag.slug, Tag.name)
        .join(Tag, Tag.id == DocumentTag.tag_id)
        .where(DocumentTag.document_id.in_(doc_ids))
    )
    out: dict[uuid.UUID, list[tuple[str, str]]] = {}
    for did, slug, name in rows:
        out.setdefault(did, []).append((slug, name))
    return out


def set_content_hash(
    docs: list[Document], tags: dict[uuid.UUID, list[tuple[str, str]]]
) -> str:
    """Deterministic hash of everything that lands in the archive / manifest (§15)."""
    payload = [
        [
            str(d.id),
            d.sha256,
            d.title,
            d.doc_date.isoformat() if d.doc_date else "",
            ",".join(sorted(slug for slug, _ in tags.get(d.id, []))),
        ]
        for d in sorted(docs, key=lambda d: str(d.id))
    ]
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


async def get_set_artifact(db: AsyncSession, set_id: uuid.UUID) -> Artifact | None:
    return await db.scalar(
        select(Artifact).where(
            Artifact.kind == ArtifactKind.set_archive,
            Artifact.source_id == set_id,
        )
    )


# --- the (re)build job -------------------------------------------------
async def build_set_archive(set_id: str | uuid.UUID) -> None:
    set_id = uuid.UUID(str(set_id))
    sm = get_sessionmaker()
    async with sm() as db:
        set_obj = await db.get(DocumentSet, set_id)
        artifact = await get_set_artifact(db, set_id)
        if set_obj is None or artifact is None:  # pragma: no cover
            return

        docs = await set_documents(db, set_id)
        tags = await tags_by_doc(db, [d.id for d in docs])
        current = set_content_hash(docs, tags)
        name_map = {did: [name for _, name in pairs] for did, pairs in tags.items()}
        total = int(
            await db.scalar(
                select(func.count()).where(DocumentSetItem.set_id == set_id)
            )
            or 0
        )

        try:
            key, size = await run_in_threadpool(
                write_document_zip, storage.set_archive_path(str(set_id)), docs, name_map
            )
        except Exception as err:  # pragma: no cover - defensive
            artifact.status = ArtifactStatus.failed
            artifact.error = str(err)[:2000]
            await db.commit()
            return

        artifact.storage_key = key
        artifact.size_bytes = size
        artifact.item_count = len(docs)
        artifact.missing_count = total - len(docs)
        artifact.content_hash = current
        artifact.snapshot = {
            "doc_ids": [str(d.id) for d in docs],
            "target_hash": current,
        }
        artifact.status = ArtifactStatus.ready
        artifact.error = None
        await db.commit()
