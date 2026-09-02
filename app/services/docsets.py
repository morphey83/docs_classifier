"""Document sets, the set-archive content hash, and the (re)build job (§15)."""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app import storage
from app.config import settings
from app.db import get_sessionmaker
from app.jobs import dispatch
from app.models import (
    Artifact,
    ArtifactKind,
    ArtifactStatus,
    Document,
    DocumentSet,
    DocumentSetItem,
    DocumentTag,
    Domain,
    DownloadLink,
    SetVisibility,
    Tag,
    User,
)
from app.rbac import ROLE_CAPS, Cap, Role
from app.services.export import write_document_zip
from app.util.time import as_aware, utcnow

LinkKind = Literal["permanent", "one_time"]


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


async def sets_of_document(
    db: AsyncSession, document_id: uuid.UUID, *, domain_id: uuid.UUID, user_id: uuid.UUID
) -> list[DocumentSet]:
    """Sets (visible to ``user_id``) that ``document_id`` currently belongs to."""
    rows = await db.scalars(
        select(DocumentSet)
        .join(DocumentSetItem, DocumentSetItem.set_id == DocumentSet.id)
        .where(
            DocumentSetItem.document_id == document_id,
            DocumentSet.domain_id == domain_id,
            or_(
                DocumentSet.visibility == SetVisibility.domain,
                DocumentSet.created_by == user_id,
            ),
        )
        .order_by(DocumentSet.name)
    )
    return list(rows)


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


# --- ensure-current (shared by the API and the bot, §15) --------------
async def ensure_current_archive(
    db: AsyncSession,
    background,
    domain: Domain,
    set_obj: DocumentSet,
    *,
    requested_by: uuid.UUID | None,
) -> tuple[Artifact, str]:
    """Compare the live set hash to the cached artifact; queue a rebuild if stale.

    ``background`` is a FastAPI ``BackgroundTasks`` from a request, or ``None``
    (the bot) — ``dispatch`` handles both.
    """
    docs = await set_documents(db, set_obj.id)
    tags = await tags_by_doc(db, [d.id for d in docs])
    current = set_content_hash(docs, tags)

    artifact = await get_set_artifact(db, set_obj.id)
    if artifact is None:
        artifact = Artifact(
            domain_id=domain.id,
            kind=ArtifactKind.set_archive,
            source_id=set_obj.id,
            status=ArtifactStatus.building,
            requested_by=requested_by,
        )
        db.add(artifact)
        await db.flush()

    ttl_days = int(
        (domain.settings or {}).get("set_archive_ttl_days", settings.set_archive_ttl_days)
    )
    path = storage.set_archive_path(str(set_obj.id))
    expired = artifact.expires_at is not None and as_aware(artifact.expires_at) <= utcnow()
    file_ok = bool(artifact.storage_key) and path.is_file()
    fresh = (
        artifact.content_hash == current
        and artifact.status == ArtifactStatus.ready
        and file_ok
        and not expired
    )
    if fresh:
        return artifact, current

    building_this = (
        artifact.status == ArtifactStatus.building
        and (artifact.snapshot or {}).get("target_hash") == current
        and not expired
    )
    if not building_this:
        artifact.status = ArtifactStatus.building
        artifact.error = None
        artifact.expires_at = utcnow() + timedelta(days=ttl_days)
        artifact.snapshot = {**(artifact.snapshot or {}), "target_hash": current}
        await db.commit()
        await dispatch(background, "build_set_archive", build_set_archive, set_id=set_obj.id)
    return artifact, current


def archive_is_ready(artifact: Artifact, current_hash: str) -> bool:
    return (
        artifact.status == ArtifactStatus.ready
        and artifact.content_hash == current_hash
        and storage.set_archive_path(str(artifact.source_id)).is_file()
    )


# --- share links (shared by the API and the bot, §15) ----------------
async def create_share_link(
    db: AsyncSession,
    background,
    *,
    domain: Domain,
    set_obj: DocumentSet,
    user: User,
    role: Role,
    kind: LinkKind,
    expires_at: datetime | None = None,
) -> DownloadLink:
    caps = ROLE_CAPS[role]
    if not (domain.settings or {}).get("allow_public_links", True):
        raise SetError("public links are disabled for this domain")
    if kind == "permanent" and Cap.write not in caps:
        raise SetError("a permanent link needs 'write' in the domain")
    if kind == "one_time" and Cap.download not in caps:
        raise SetError("a link needs 'download' in the domain")

    artifact, _ = await ensure_current_archive(
        db, background, domain, set_obj, requested_by=user.id
    )
    link = DownloadLink(
        artifact_id=artifact.id,
        token=secrets.token_urlsafe(24),
        max_downloads=1 if kind == "one_time" else None,
        expires_at=expires_at,
        created_by=user.id,
    )
    db.add(link)
    await db.flush()
    return link


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
