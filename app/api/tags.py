"""The global tag pool (§7 rev 2). Tags aren't domain-owned; anyone signed in
can rename / recolour / merge. Deletion is automatic (nightly orphan sweep)."""

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


def _out(tag: Tag, usage: int = 0) -> TagOut:
    o = TagOut.model_validate(tag)
    o.usage_count = usage
    return o


async def _load(db: AsyncSession, tag_id: uuid.UUID) -> Tag:
    tag = await db.get(Tag, tag_id)
    if tag is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "tag not found")
    return tag


@router.get("/tags/all", response_model=list[TagOut])
async def list_all_tags(
    _: User = Depends(get_current_user), db: AsyncSession = Depends(get_session)
) -> list[TagOut]:
    return [_out(tag, usage) for tag, usage in await svc.list_tags(db)]


@router.patch("/tags/{tag_id}", response_model=TagOut)
async def update_tag(
    tag_id: uuid.UUID,
    body: TagUpdate,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> TagOut:
    tag = await _load(db, tag_id)
    try:
        if body.name is not None:
            await svc.rename_tag(db, tag, body.name)
        if body.color is not None:
            await svc.recolor_tag(db, tag, body.color)
    except svc.TagError as err:
        raise HTTPException(status.HTTP_409_CONFLICT, str(err)) from err
    return _out(tag)


@router.post("/tags/{tag_id}/merge", status_code=status.HTTP_200_OK)
async def merge_tag(
    tag_id: uuid.UUID,
    body: TagMerge,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    source = await _load(db, tag_id)
    target = await _load(db, body.into)
    try:
        await svc.merge_tags(db, source=source, target=target)
    except svc.TagError as err:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(err)) from err
    return {"merged_into": str(target.id)}
