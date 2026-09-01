"""Per-domain tag vocabulary and document tagging."""

from __future__ import annotations

import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Document, DocumentTag, Tag, User
from app.util.slug import slugify


class TagError(ValueError):
    pass


async def list_tags(db: AsyncSession, domain_id: uuid.UUID) -> list[tuple[Tag, int]]:
    rows = await db.execute(
        select(Tag, func.count(DocumentTag.document_id))
        .outerjoin(DocumentTag, DocumentTag.tag_id == Tag.id)
        .where(Tag.domain_id == domain_id)
        .group_by(Tag.id)
        .order_by(Tag.name)
    )
    return list(rows.all())


async def get_or_create_tag(
    db: AsyncSession, domain_id: uuid.UUID, name: str, *, actor: User
) -> Tag:
    name = name.strip()
    if not name:
        raise TagError("tag name is empty")
    slug = slugify(name)
    tag = await db.scalar(
        select(Tag).where(Tag.domain_id == domain_id, Tag.slug == slug)
    )
    if tag is None:
        tag = Tag(domain_id=domain_id, name=name, slug=slug, created_by=actor.id)
        db.add(tag)
        await db.flush()
    return tag


async def create_tag(
    db: AsyncSession,
    domain_id: uuid.UUID,
    *,
    name: str,
    color: str | None,
    description: str | None,
    actor: User,
) -> Tag:
    slug = slugify(name.strip())
    if await db.scalar(select(Tag.id).where(Tag.domain_id == domain_id, Tag.slug == slug)):
        raise TagError(f"tag '{name}' already exists")
    tag = Tag(
        domain_id=domain_id,
        name=name.strip(),
        slug=slug,
        color=color,
        description=description,
        created_by=actor.id,
    )
    db.add(tag)
    await db.flush()
    return tag


async def update_tag(
    db: AsyncSession, tag: Tag, *, name: str | None, color: str | None, description: str | None
) -> Tag:
    if name is not None:
        new_slug = slugify(name.strip())
        clash = await db.scalar(
            select(Tag.id).where(
                Tag.domain_id == tag.domain_id, Tag.slug == new_slug, Tag.id != tag.id
            )
        )
        if clash:
            raise TagError("another tag already uses that name")
        tag.name = name.strip()
        tag.slug = new_slug
    if color is not None:
        tag.color = color or None
    if description is not None:
        tag.description = description or None
    await db.flush()
    return tag


async def delete_tag(db: AsyncSession, tag: Tag) -> None:
    await db.execute(delete(DocumentTag).where(DocumentTag.tag_id == tag.id))
    await db.delete(tag)


async def merge_tags(db: AsyncSession, *, source: Tag, target: Tag) -> None:
    if source.id == target.id:
        raise TagError("cannot merge a tag into itself")
    if source.domain_id != target.domain_id:
        raise TagError("tags belong to different domains")
    already = set(
        await db.scalars(
            select(DocumentTag.document_id).where(DocumentTag.tag_id == target.id)
        )
    )
    links = await db.scalars(
        select(DocumentTag).where(DocumentTag.tag_id == source.id)
    )
    for link in links:
        if link.document_id in already:
            await db.delete(link)
        else:
            link.tag_id = target.id
    await db.flush()
    await db.delete(source)


async def set_document_tags(
    db: AsyncSession, document: Document, tag_ids: list[uuid.UUID], *, actor: User
) -> None:
    valid = set(
        await db.scalars(
            select(Tag.id).where(Tag.domain_id == document.domain_id, Tag.id.in_(tag_ids))
        )
    )
    missing = set(tag_ids) - valid
    if missing:
        raise TagError(f"unknown tag ids: {sorted(map(str, missing))}")

    current = set(
        await db.scalars(
            select(DocumentTag.tag_id).where(DocumentTag.document_id == document.id)
        )
    )
    to_add = valid - current
    to_remove = current - valid
    if to_remove:
        await db.execute(
            delete(DocumentTag).where(
                DocumentTag.document_id == document.id,
                DocumentTag.tag_id.in_(to_remove),
            )
        )
    for tid in to_add:
        db.add(DocumentTag(document_id=document.id, tag_id=tid, assigned_by=actor.id))
    await db.flush()
