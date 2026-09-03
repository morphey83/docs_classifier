"""User-owned document sets: saved filters + explicit adds, resolved live,
the shareable archive cache, and share links (§15 rev 4)."""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import func, select
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
    DocumentSetFilter,
    DocumentSetItem,
    DocumentTag,
    DownloadLink,
    Tag,
    User,
)
from app.rbac import ROLE_CAPS, Cap, Role
from app.services import domains as domains_svc
from app.services.export import write_document_zip
from app.services.search import SearchFilters, search_documents
from app.util.time import as_aware, utcnow

LinkKind = Literal["permanent", "one_time"]
SetView = Literal["share", "full"]


class SetError(ValueError):
    pass


# --- CRUD ------------------------------------------------------------------
async def list_sets(db: AsyncSession, owner_id: uuid.UUID) -> list[DocumentSet]:
    rows = await db.scalars(
        select(DocumentSet)
        .where(DocumentSet.owner_id == owner_id)
        .order_by(DocumentSet.updated_at.desc())
    )
    return list(rows)


async def get_owned_set(
    db: AsyncSession, set_id: uuid.UUID, owner_id: uuid.UUID
) -> DocumentSet | None:
    s = await db.get(DocumentSet, set_id)
    return s if s is not None and s.owner_id == owner_id else None


async def create_set(
    db: AsyncSession,
    owner: User,
    *,
    name: str,
    description: str | None = None,
    document_ids: list[uuid.UUID] | None = None,
) -> DocumentSet:
    s = DocumentSet(
        owner_id=owner.id, name=name.strip() or "Набор", description=description or None
    )
    db.add(s)
    await db.flush()
    if document_ids:
        await add_items(db, s, document_ids, actor=owner)
    return s


async def rename_set(
    db: AsyncSession, s: DocumentSet, *, name: str | None, description: str | None
) -> None:
    if name is not None:
        s.name = name.strip() or s.name
    if description is not None:
        s.description = description or None
    await db.flush()


async def delete_set(db: AsyncSession, s: DocumentSet) -> None:
    artifact = await get_set_artifact(db, s.id)
    if artifact is not None:
        storage.set_archive_path(str(s.id)).unlink(missing_ok=True)
        await db.delete(artifact)  # cascades its download_links
    await db.delete(s)  # cascades filters + items


# --- filters -------------------------------------------------------------
async def add_filter(
    db: AsyncSession, s: DocumentSet, f: SearchFilters, *, description: str
) -> DocumentSetFilter:
    pos = int(
        await db.scalar(
            select(func.coalesce(func.max(DocumentSetFilter.position), 0)).where(
                DocumentSetFilter.set_id == s.id
            )
        )
        or 0
    )
    row = DocumentSetFilter(
        set_id=s.id, position=pos + 1, filter=f.to_dict(), description=description[:500]
    )
    db.add(row)
    s.updated_at = utcnow()
    await db.flush()
    return row


async def remove_filter(db: AsyncSession, s: DocumentSet, filter_id: uuid.UUID) -> bool:
    row = await db.get(DocumentSetFilter, filter_id)
    if row is None or row.set_id != s.id:
        return False
    await db.delete(row)
    s.updated_at = utcnow()
    await db.flush()
    return True


async def list_filters(db: AsyncSession, set_id: uuid.UUID) -> list[DocumentSetFilter]:
    rows = await db.scalars(
        select(DocumentSetFilter)
        .where(DocumentSetFilter.set_id == set_id)
        .order_by(DocumentSetFilter.position)
    )
    return list(rows)


# --- explicit items -----------------------------------------------------
async def add_items(
    db: AsyncSession, s: DocumentSet, doc_ids: list[uuid.UUID], *, actor: User
) -> int:
    dl_domains = await _download_domain_ids(db, s.owner_id)
    if not dl_domains or not doc_ids:
        return 0
    valid = set(
        await db.scalars(
            select(Document.id).where(
                Document.id.in_(doc_ids),
                Document.deleted_at.is_(None),
                Document.domain_id.in_(dl_domains),
            )
        )
    )
    present = set(
        await db.scalars(
            select(DocumentSetItem.document_id).where(DocumentSetItem.set_id == s.id)
        )
    )
    pos = int(
        await db.scalar(
            select(func.coalesce(func.max(DocumentSetItem.position), 0)).where(
                DocumentSetItem.set_id == s.id
            )
        )
        or 0
    )
    added = 0
    for did in doc_ids:
        if did not in valid or did in present:
            continue
        pos += 1
        db.add(DocumentSetItem(set_id=s.id, document_id=did, added_by=actor.id, position=pos))
        present.add(did)
        added += 1
    if added:
        s.updated_at = utcnow()
    await db.flush()
    return added


async def remove_item(db: AsyncSession, s: DocumentSet, document_id: uuid.UUID) -> bool:
    item = await db.get(DocumentSetItem, (s.id, document_id))
    if item is None:
        return False
    await db.delete(item)
    s.updated_at = utcnow()
    await db.flush()
    return True


async def sets_containing_document(
    db: AsyncSession, document_id: uuid.UUID, owner_id: uuid.UUID
) -> set[uuid.UUID]:
    """Ids of the user's sets that *explicitly* include ``document_id``."""
    rows = await db.scalars(
        select(DocumentSetItem.set_id)
        .join(DocumentSet, DocumentSet.id == DocumentSetItem.set_id)
        .where(DocumentSetItem.document_id == document_id, DocumentSet.owner_id == owner_id)
    )
    return set(rows)


# --- resolve -------------------------------------------------------------
async def _download_domain_ids(db: AsyncSession, owner_id: uuid.UUID) -> list[uuid.UUID]:
    owner = await db.get(User, owner_id)
    if owner is None:
        return []
    out = []
    for domain, member in await domains_svc.list_memberships(db, owner):
        if Cap.download in ROLE_CAPS[Role(member.role)]:
            out.append(domain.id)
    return out


async def resolve_set(
    db: AsyncSession, s: DocumentSet, *, view: SetView
) -> list[Document]:
    """The ordered document list a set currently produces (§15)."""
    dl_domains = set(await _download_domain_ids(db, s.owner_id))
    if not dl_domains:
        return []

    # 1. explicit items, in their order
    explicit = list(
        await db.scalars(
            select(Document)
            .join(DocumentSetItem, DocumentSetItem.document_id == Document.id)
            .where(DocumentSetItem.set_id == s.id, Document.deleted_at.is_(None))
            .order_by(DocumentSetItem.position, DocumentSetItem.added_at)
        )
    )
    seen = {d.id for d in explicit}

    # 2. every filter's matches
    cap = settings.set_max_docs + 1
    filter_docs: list[Document] = []
    for fr in await list_filters(db, s.id):
        sf = SearchFilters.from_dict(fr.filter)
        scope = [d for d in (sf.domain_ids or dl_domains) if d in dl_domains]
        if not scope:
            continue
        sf.page, sf.page_size = 1, cap
        docs, _total, _facets = await search_documents(db, scope, sf)
        for d in docs:
            if d.id not in seen:
                seen.add(d.id)
                filter_docs.append(d)
    filter_docs.sort(key=lambda d: (as_aware(d.uploaded_at), str(d.id)), reverse=True)

    result = [
        d
        for d in (*explicit, *filter_docs)
        if d.domain_id in dl_domains and d.deleted_at is None
    ]
    if view == "share":
        result = [d for d in result if d.is_public]
    return result


async def _tags_by_doc(
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


# --- shareable archive (ensure-current) --------------------------------
async def get_set_artifact(db: AsyncSession, set_id: uuid.UUID) -> Artifact | None:
    return await db.scalar(
        select(Artifact).where(
            Artifact.kind == ArtifactKind.set_archive, Artifact.source_id == set_id
        )
    )


async def ensure_current_archive(
    db: AsyncSession, background, s: DocumentSet, *, requested_by: uuid.UUID | None
) -> tuple[Artifact, str]:
    docs = await resolve_set(db, s, view="share")
    tags = await _tags_by_doc(db, [d.id for d in docs])
    current = set_content_hash(docs, tags)

    artifact = await get_set_artifact(db, s.id)
    if artifact is None:
        artifact = Artifact(
            domain_id=None,
            kind=ArtifactKind.set_archive,
            source_id=s.id,
            status=ArtifactStatus.building,
            requested_by=requested_by,
        )
        db.add(artifact)
        await db.flush()

    path = storage.set_archive_path(str(s.id))
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
        artifact.expires_at = utcnow() + timedelta(days=settings.set_archive_ttl_days)
        artifact.snapshot = {**(artifact.snapshot or {}), "target_hash": current}
        await db.commit()
        await dispatch(background, "build_set_archive", build_set_archive, set_id=s.id)
    return artifact, current


def archive_is_ready(artifact: Artifact, current_hash: str) -> bool:
    return (
        artifact.status == ArtifactStatus.ready
        and artifact.content_hash == current_hash
        and storage.set_archive_path(str(artifact.source_id)).is_file()
    )


async def build_set_archive(set_id: str | uuid.UUID) -> None:
    set_id = uuid.UUID(str(set_id))
    sm = get_sessionmaker()
    async with sm() as db:
        s = await db.get(DocumentSet, set_id)
        artifact = await get_set_artifact(db, set_id)
        if s is None or artifact is None:  # pragma: no cover
            return

        docs = await resolve_set(db, s, view="share")
        tags = await _tags_by_doc(db, [d.id for d in docs])
        current = set_content_hash(docs, tags)
        name_map = {did: [name for _, name in pairs] for did, pairs in tags.items()}

        over = _limit_error(docs)
        if over is not None:
            artifact.status = ArtifactStatus.failed
            artifact.error = over
            await db.commit()
            return

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
        artifact.missing_count = 0
        artifact.content_hash = current
        artifact.snapshot = {"doc_ids": [str(d.id) for d in docs], "target_hash": current}
        artifact.status = ArtifactStatus.ready
        artifact.error = None
        await db.commit()


def _limit_error(docs: list[Document]) -> str | None:
    if len(docs) > settings.set_max_docs:
        return (
            f"в наборе {len(docs)} документов — больше лимита {settings.set_max_docs}. "
            "Сузьте фильтр."
        )
    total = sum(d.size_bytes for d in docs)
    if total > settings.set_archive_max_bytes:
        gb = settings.set_archive_max_bytes / 1024**3
        return f"суммарный размер документов больше лимита ({gb:.0f} ГиБ). Сузьте фильтр."
    return None


# --- «Полная выгрузка» — the owner's full ad-hoc export ---------------
async def start_full_export(
    db: AsyncSession, background, s: DocumentSet, owner: User
) -> Artifact:
    docs = await resolve_set(db, s, view="full")
    over = _limit_error(docs)
    if over is not None:
        raise SetError(over)
    artifact = Artifact(
        domain_id=None,
        kind=ArtifactKind.adhoc_export,
        source_id=s.id,
        status=ArtifactStatus.building,
        item_count=len(docs),
        snapshot={"doc_ids": [str(d.id) for d in docs]},
        requested_by=owner.id,
        expires_at=utcnow() + timedelta(hours=settings.export_ttl_hours),
    )
    db.add(artifact)
    await db.flush()
    aid = artifact.id
    await db.commit()
    from app.services.export import build_artifact

    await dispatch(background, "build_artifact", build_artifact, artifact_id=aid)
    return artifact


# --- share links -------------------------------------------------------
async def create_share_link(
    db: AsyncSession,
    background,
    *,
    s: DocumentSet,
    user: User,
    kind: LinkKind,
    expires_at: datetime | None = None,
) -> DownloadLink:
    artifact, _ = await ensure_current_archive(db, background, s, requested_by=user.id)
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


async def revoke_link(db: AsyncSession, link: DownloadLink) -> None:
    if link.revoked_at is None:
        link.revoked_at = utcnow()
    await db.flush()


async def links_of_set(db: AsyncSession, set_id: uuid.UUID) -> list[DownloadLink]:
    artifact = await get_set_artifact(db, set_id)
    if artifact is None:
        return []
    rows = await db.scalars(
        select(DownloadLink)
        .where(DownloadLink.artifact_id == artifact.id, DownloadLink.revoked_at.is_(None))
        .order_by(DownloadLink.created_at.desc())
    )
    return list(rows)
