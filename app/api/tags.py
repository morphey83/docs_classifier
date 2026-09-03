"""The global tag pool (§7 rev 2). Tags aren't domain-owned. Names are fixed;
each user sets their own colour. Merge moves a tag's documents onto another tag
(only in domains the caller owns). Deletion is automatic (nightly orphan sweep)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Tag, User
from app.schemas.tags import TagMerge, TagOut, TagUpdate
from app.security import get_current_user
from app.services import tags as svc

router = APIRouter(tags=["tags"])


def _out(tag: Tag, usage: int = 0, color: str | None = None) -> TagOut:
    o = TagOut.model_validate(tag)
    o.usage_count = usage
    o.color = color
    return o


async def _load(db: AsyncSession, tag_id: uuid.UUID) -> Tag:
    tag = await db.get(Tag, tag_id)
    if tag is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "tag not found")
    return tag


@router.get("/tags/all", response_model=list[TagOut])
async def list_all_tags(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_session)
) -> list[TagOut]:
    colors = await svc.tag_colors(db, user.id)
    return [_out(tag, usage, colors.get(tag.id)) for tag, usage in await svc.list_tags(db)]


@router.patch("/tags/{tag_id}", response_model=TagOut)
async def update_tag(
    tag_id: uuid.UUID,
    body: TagUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> TagOut:
    tag = await _load(db, tag_id)
    await svc.set_tag_color(db, user_id=user.id, tag_id=tag_id, color=body.color or None)
    return _out(tag, 0, body.color or None)


@router.post("/tags/{tag_id}/merge", status_code=status.HTTP_200_OK)
async def merge_tag(
    tag_id: uuid.UUID,
    body: TagMerge,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    source = await _load(db, tag_id)
    target = await _load(db, body.into)
    try:
        await svc.merge_tags(db, source=source, target=target, owner_id=user.id)
    except svc.TagError as err:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(err)) from err
    return {"merged_into": str(target.id)}
