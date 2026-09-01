"""Per-domain tag vocabulary endpoints."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import DomainCtx, require
from app.models import Tag
from app.rbac import Cap
from app.schemas.tags import TagCreate, TagMerge, TagOut, TagUpdate
from app.services import tags as svc

router = APIRouter(tags=["tags"])


def _out(tag: Tag, usage: int = 0) -> TagOut:
    o = TagOut.model_validate(tag)
    o.usage_count = usage
    return o


async def _load_tag(db: AsyncSession, ctx: DomainCtx, tag_id: uuid.UUID) -> Tag:
    tag = await db.get(Tag, tag_id)
    if tag is None or tag.domain_id != ctx.domain.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "tag not found")
    return tag


@router.get("/domains/{domain_id}/tags", response_model=list[TagOut])
async def list_tags(
    ctx: DomainCtx = Depends(require(Cap.view)), db: AsyncSession = Depends(get_session)
) -> list[TagOut]:
    return [_out(tag, usage) for tag, usage in await svc.list_tags(db, ctx.domain.id)]


@router.post(
    "/domains/{domain_id}/tags", response_model=TagOut, status_code=status.HTTP_201_CREATED
)
async def create_tag(
    body: TagCreate,
    ctx: DomainCtx = Depends(require(Cap.write)),
    db: AsyncSession = Depends(get_session),
) -> TagOut:
    try:
        tag = await svc.create_tag(
            db,
            ctx.domain.id,
            name=body.name,
            color=body.color,
            description=body.description,
            actor=ctx.user,
        )
    except svc.TagError as err:
        raise HTTPException(status.HTTP_409_CONFLICT, str(err)) from err
    return _out(tag)


@router.patch("/domains/{domain_id}/tags/{tag_id}", response_model=TagOut)
async def update_tag(
    tag_id: uuid.UUID,
    body: TagUpdate,
    ctx: DomainCtx = Depends(require(Cap.manage)),
    db: AsyncSession = Depends(get_session),
) -> TagOut:
    tag = await _load_tag(db, ctx, tag_id)
    try:
        tag = await svc.update_tag(
            db, tag, name=body.name, color=body.color, description=body.description
        )
    except svc.TagError as err:
        raise HTTPException(status.HTTP_409_CONFLICT, str(err)) from err
    return _out(tag)


@router.delete("/domains/{domain_id}/tags/{tag_id}")
async def delete_tag(
    tag_id: uuid.UUID,
    ctx: DomainCtx = Depends(require(Cap.manage)),
    db: AsyncSession = Depends(get_session),
) -> Response:
    tag = await _load_tag(db, ctx, tag_id)
    await svc.delete_tag(db, tag)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/domains/{domain_id}/tags/{tag_id}/merge", status_code=status.HTTP_200_OK)
async def merge_tag(
    tag_id: uuid.UUID,
    body: TagMerge,
    ctx: DomainCtx = Depends(require(Cap.manage)),
    db: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    source = await _load_tag(db, ctx, tag_id)
    target = await _load_tag(db, ctx, body.into)
    try:
        await svc.merge_tags(db, source=source, target=target)
    except svc.TagError as err:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(err)) from err
    return {"merged_into": str(target.id)}
