"""The global tag pool and document tagging (§7 rev 2).

Tags are not owned by a domain. A tag exists while ≥1 document carries it;
:func:`sweep_orphan_tags` (nightly cleanup) removes ones that drop to zero.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Document, DocumentTag, Domain, Tag, User
from app.util.slug import slugify


class TagError(ValueError):
    pass


# --- reads ---------------------------------------------------------------
async def list_tags(
    db: AsyncSession, *, q: str | None = None, include_orphans: bool = False
) -> list[tuple[Tag, int]]:
    """Tags with their live document count, most-used first. Tags on zero
    documents are hidden (they are limbo — waiting for the nightly
    :func:`sweep_orphan_tags`); pass ``include_orphans`` to see them anyway."""
    cnt = func.count(DocumentTag.document_id)
    stmt = (
        select(Tag, cnt)
        .outerjoin(DocumentTag, DocumentTag.tag_id == Tag.id)
        .group_by(Tag.id)
        .order_by(cnt.desc(), Tag.name)
    )
    if not include_orphans:
        stmt = stmt.having(cnt > 0)
    if q and q.strip():
        stmt = stmt.where(Tag.name.ilike(f"%{q.strip()}%"))
    return list((await db.execute(stmt)).all())


async def suggest_tags(
    db: AsyncSession, domain_ids: Sequence[uuid.UUID], *, limit: int | None = 14
) -> list[tuple[str, int]]:
    """``(name, document count)`` for the tags on non-deleted documents in
    ``domain_ids``, most-used first. Feeds the 'частые теги' chips and the
    ``GET /api/tags`` filter-picker options."""
    if not domain_ids:
        return []
    cnt = func.count(func.distinct(DocumentTag.document_id))
    stmt = (
        select(Tag.name, cnt)
        .join(DocumentTag, DocumentTag.tag_id == Tag.id)
        .join(Document, Document.id == DocumentTag.document_id)
        .where(Document.domain_id.in_(domain_ids), Document.deleted_at.is_(None))
        .group_by(Tag.id, Tag.name)
        .order_by(cnt.desc(), Tag.name)
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return [tuple(row) for row in await db.execute(stmt)]


async def tag_names(db: AsyncSession, doc_id: uuid.UUID) -> list[str]:
    rows = await db.scalars(
        select(Tag.name)
        .join(DocumentTag, DocumentTag.tag_id == Tag.id)
        .where(DocumentTag.document_id == doc_id)
        .order_by(Tag.name)
    )
    return list(rows)


# --- create / normalise ----------------------------------------------
async def get_or_create_tag(db: AsyncSession, name: str, *, actor: User) -> Tag:
    name = name.strip()
    if not name:
        raise TagError("tag name is empty")
    slug = slugify(name)
    tag = await db.scalar(select(Tag).where(Tag.slug == slug))
    if tag is None:
        tag = Tag(name=name, slug=slug, created_by=actor.id)
        db.add(tag)
        await db.flush()
    return tag


async def resolve_names(db: AsyncSession, names: Sequence[str], *, actor: User) -> list[uuid.UUID]:
    out: list[uuid.UUID] = []
    for raw in names:
        if raw.strip():
            out.append((await get_or_create_tag(db, raw, actor=actor)).id)
    return out


# --- edit ---------------------------------------------------------------
async def rename_tag(db: AsyncSession, tag: Tag, name: str) -> Tag:
    name = name.strip()
    if not name:
        raise TagError("tag name is empty")
    new_slug = slugify(name)
    if new_slug != tag.slug and await db.scalar(select(Tag.id).where(Tag.slug == new_slug)):
        raise TagError("another tag already uses that name")
    tag.name, tag.slug = name, new_slug
    await db.flush()
    return tag


async def recolor_tag(db: AsyncSession, tag: Tag, color: str | None) -> Tag:
    tag.color = color or None
    await db.flush()
    return tag


async def merge_tags(
    db: AsyncSession, *, source: Tag, target: Tag, owner_id: uuid.UUID
) -> int:
    """Move ``source`` → ``target`` on every document in a domain ``owner_id``
    owns (dedup). Documents in other people's domains keep ``source``. The tag
    row itself is never deleted here — like any tag that drops to zero documents
    it waits for the nightly :func:`sweep_orphan_tags`. Returns the number of
    documents changed."""
    if source.id == target.id:
        raise TagError("cannot merge a tag into itself")

    owned_docs = select(Document.id).where(
        Document.domain_id.in_(select(Domain.id).where(Domain.owner_id == owner_id))
    )
    already = set(
        await db.scalars(select(DocumentTag.document_id).where(DocumentTag.tag_id == target.id))
    )
    changed = 0
    for link in await db.scalars(
        select(DocumentTag).where(
            DocumentTag.tag_id == source.id, DocumentTag.document_id.in_(owned_docs)
        )
    ):
        if link.document_id in already:
            await db.delete(link)
        else:
            link.tag_id = target.id
        changed += 1
    await db.flush()
    return changed


# --- assignment -----------------------------------------------------
async def set_document_tags(
    db: AsyncSession, document: Document, tag_ids: list[uuid.UUID], *, actor: User
) -> None:
    valid = set(await db.scalars(select(Tag.id).where(Tag.id.in_(tag_ids))))
    missing = set(tag_ids) - valid
    if missing:
        raise TagError(f"unknown tag ids: {sorted(map(str, missing))}")

    current = set(
        await db.scalars(select(DocumentTag.tag_id).where(DocumentTag.document_id == document.id))
    )
    to_remove = current - valid
    if to_remove:
        await db.execute(
            delete(DocumentTag).where(
                DocumentTag.document_id == document.id, DocumentTag.tag_id.in_(to_remove)
            )
        )
    for tid in valid - current:
        db.add(DocumentTag(document_id=document.id, tag_id=tid, assigned_by=actor.id))
    await db.flush()


async def add_tags_to_documents(
    db: AsyncSession, doc_ids: Sequence[uuid.UUID], tag_ids: Sequence[uuid.UUID], *, actor: User
) -> int:
    """Additive bulk tagging. A tag a document already has is silently kept."""
    if not doc_ids or not tag_ids:
        return 0
    tag_ids = set(await db.scalars(select(Tag.id).where(Tag.id.in_(list(tag_ids)))))
    rows = await db.execute(
        select(DocumentTag.document_id, DocumentTag.tag_id).where(
            DocumentTag.document_id.in_(list(doc_ids)), DocumentTag.tag_id.in_(list(tag_ids))
        )
    )
    existing = set(rows.all())
    added = 0
    for did in doc_ids:
        for tid in tag_ids:
            if (did, tid) not in existing:
                db.add(DocumentTag(document_id=did, tag_id=tid, assigned_by=actor.id))
                added += 1
    await db.flush()
    return added


# --- cleanup ------------------------------------------------------
async def sweep_orphan_tags(db: AsyncSession) -> int:
    """Delete tags no document references any more. Returns the count."""
    orphans = list(
        await db.scalars(
            select(Tag.id).outerjoin(DocumentTag, DocumentTag.tag_id == Tag.id).where(
                DocumentTag.tag_id.is_(None)
            )
        )
    )
    if orphans:
        await db.execute(delete(Tag).where(Tag.id.in_(orphans)))
    return len(orphans)
