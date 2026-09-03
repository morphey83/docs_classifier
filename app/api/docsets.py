"""User-owned document sets, their shareable archive, and public links (§15)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.config import settings
from app.db import get_session
from app.models import (
    Artifact,
    ArtifactKind,
    ArtifactStatus,
    DocumentSet,
    DownloadLink,
    User,
)
from app.schemas.docsets import (
    ArchiveStatusOut,
    FilterAdd,
    FilterOut,
    LinkCreate,
    LinkOut,
    SetCreate,
    SetDetail,
    SetItemsAdd,
    SetOut,
    SetUpdate,
)
from app.schemas.exports import ArtifactOut
from app.security import get_current_user
from app.services import docsets as svc
from app.services.search import SearchFilters
from app.util import ratelimit
from app.util.time import as_aware, utcnow
from app.util.urls import absolute_url

router = APIRouter(tags=["sets"])
# Mounted at the site root (not /api) — share links must stay short.
public_router = APIRouter(tags=["share"])


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?")


async def _owned(db: AsyncSession, set_id: uuid.UUID, user: User) -> DocumentSet:
    s = await svc.get_owned_set(db, set_id, user.id)
    if s is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "set not found")
    return s


def _archive_filename(s: DocumentSet) -> str:
    safe = "".join(c if c.isalnum() or c in " -_." else "_" for c in s.name).strip()
    return f"{safe or 'set'} {utcnow():%Y-%m-%d}.zip"


async def _detail(db: AsyncSession, s: DocumentSet) -> SetDetail:
    await db.refresh(s)
    filters = await svc.list_filters(db, s.id)
    resolved = await svc.resolve_set(db, s, view="full")
    from sqlalchemy import func, select

    from app.models import DocumentSetItem

    item_count = int(
        await db.scalar(
            select(func.count()).where(DocumentSetItem.set_id == s.id)
        )
        or 0
    )
    return SetDetail(
        **SetOut.model_validate(s).model_dump(),
        filters=[FilterOut.model_validate(f) for f in filters],
        item_count=item_count,
        resolved_count=len(resolved),
    )


# --- set CRUD --------------------------------------------------------
@router.post("/sets", response_model=SetOut, status_code=status.HTTP_201_CREATED)
async def create_set(
    body: SetCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> SetOut:
    s = await svc.create_set(
        db, user, name=body.name, description=body.description, document_ids=body.document_ids
    )
    await db.refresh(s)
    return SetOut.model_validate(s)


@router.get("/sets", response_model=list[SetOut])
async def list_sets(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_session)
) -> list[SetOut]:
    return [SetOut.model_validate(s) for s in await svc.list_sets(db, user.id)]


@router.get("/sets/{set_id}", response_model=SetDetail)
async def get_set(
    set_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> SetDetail:
    return await _detail(db, await _owned(db, set_id, user))


@router.patch("/sets/{set_id}", response_model=SetOut)
async def update_set(
    set_id: uuid.UUID,
    body: SetUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> SetOut:
    s = await _owned(db, set_id, user)
    await svc.rename_set(db, s, name=body.name, description=body.description)
    await db.refresh(s)
    return SetOut.model_validate(s)


@router.delete("/sets/{set_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_set(
    set_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    await svc.delete_set(db, await _owned(db, set_id, user))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- filters --------------------------------------------------------
@router.post("/sets/{set_id}/filters", response_model=SetDetail)
async def add_filter(
    set_id: uuid.UUID,
    body: FilterAdd,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> SetDetail:
    s = await _owned(db, set_id, user)
    await svc.add_filter(db, s, SearchFilters.from_dict(body.filter), description=body.description)
    return await _detail(db, s)


@router.delete("/sets/{set_id}/filters/{filter_id}", response_model=SetDetail)
async def remove_filter(
    set_id: uuid.UUID,
    filter_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> SetDetail:
    s = await _owned(db, set_id, user)
    await svc.remove_filter(db, s, filter_id)
    return await _detail(db, s)


# --- explicit items -----------------------------------------------
@router.post("/sets/{set_id}/items", response_model=SetDetail)
async def add_items(
    set_id: uuid.UUID,
    body: SetItemsAdd,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> SetDetail:
    s = await _owned(db, set_id, user)
    await svc.add_items(db, s, body.document_ids, actor=user)
    return await _detail(db, s)


@router.delete("/sets/{set_id}/items/{document_id}", response_model=SetDetail)
async def remove_item(
    set_id: uuid.UUID,
    document_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> SetDetail:
    s = await _owned(db, set_id, user)
    await svc.remove_item(db, s, document_id)
    return await _detail(db, s)


# --- shareable archive -------------------------------------------
def _archive_status(artifact: Artifact, current: str) -> ArchiveStatusOut:
    ready = artifact.status == ArtifactStatus.ready and artifact.content_hash == current
    return ArchiveStatusOut(
        status=str(artifact.status),
        ready=ready,
        stale=not ready and artifact.status != ArtifactStatus.failed,
        item_count=artifact.item_count,
        missing_count=artifact.missing_count,
        size_bytes=artifact.size_bytes,
        expires_at=artifact.expires_at,
        error=artifact.error,
    )


@router.get("/sets/{set_id}/archive", response_model=ArchiveStatusOut)
async def archive_status(
    set_id: uuid.UUID,
    background: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> ArchiveStatusOut:
    s = await _owned(db, set_id, user)
    artifact, current = await svc.ensure_current_archive(db, background, s, requested_by=user.id)
    return _archive_status(artifact, current)


@router.get("/sets/{set_id}/archive/download")
async def download_archive(
    set_id: uuid.UUID,
    background: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    s = await _owned(db, set_id, user)
    artifact, current = await svc.ensure_current_archive(db, background, s, requested_by=user.id)
    if svc.archive_is_ready(artifact, current):
        return FileResponse(
            storage.set_archive_path(str(set_id)),
            media_type="application/zip",
            filename=_archive_filename(s),
        )
    return JSONResponse(
        {"status": "building", "retry_after": 2}, status_code=status.HTTP_202_ACCEPTED
    )


@router.post(
    "/sets/{set_id}/export",
    response_model=ArtifactOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def full_export(
    set_id: uuid.UUID,
    background: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> ArtifactOut:
    s = await _owned(db, set_id, user)
    try:
        artifact = await svc.start_full_export(db, background, s, user)
    except svc.SetError as err:
        raise HTTPException(status.HTTP_409_CONFLICT, str(err)) from err
    return ArtifactOut.model_validate(artifact)


# --- share links ------------------------------------------------
def _link_out(link: DownloadLink) -> LinkOut:
    return LinkOut(
        id=link.id,
        token=link.token,
        url=absolute_url(f"/d/{link.token}"),
        kind="one_time" if link.max_downloads == 1 else "permanent",
        max_downloads=link.max_downloads,
        download_count=link.download_count,
        expires_at=link.expires_at,
        revoked_at=link.revoked_at,
        last_downloaded_at=link.last_downloaded_at,
        created_at=link.created_at,
    )


@router.post("/sets/{set_id}/links", response_model=LinkOut, status_code=status.HTTP_201_CREATED)
async def create_link(
    set_id: uuid.UUID,
    body: LinkCreate,
    background: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> LinkOut:
    s = await _owned(db, set_id, user)
    try:
        link = await svc.create_share_link(
            db, background, s=s, user=user, kind=body.kind, expires_at=body.expires_at
        )
    except svc.SetError as err:
        raise HTTPException(status.HTTP_409_CONFLICT, str(err)) from err
    return _link_out(link)


@router.get("/sets/{set_id}/links", response_model=list[LinkOut])
async def list_links(
    set_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> list[LinkOut]:
    s = await _owned(db, set_id, user)
    return [_link_out(link) for link in await svc.links_of_set(db, s.id)]


@router.delete("/links/{link_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_link(
    link_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    link = await db.get(DownloadLink, link_id)
    if link is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "link not found")
    artifact = await db.get(Artifact, link.artifact_id)
    s = await db.get(DocumentSet, artifact.source_id) if artifact else None
    if s is None or s.owner_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "link not found")
    await svc.revoke_link(db, link)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- public download (no auth, mounted at root) ---------------
@public_router.get("/d/{token}")
async def public_download(
    token: str,
    request: Request,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_session),
) -> Response:
    from sqlalchemy import select

    ip = _client_ip(request)
    if not ratelimit.check(f"d:{ip}", settings.public_download_rate_per_min):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "slow down")

    link = await db.scalar(select(DownloadLink).where(DownloadLink.token == token))
    if link is None or link.revoked_at is not None or link.mode != "archive":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "link not found")
    if link.expires_at is not None and as_aware(link.expires_at) <= utcnow():
        raise HTTPException(status.HTTP_410_GONE, "this link has expired")
    if link.max_downloads is not None and link.download_count >= link.max_downloads:
        raise HTTPException(status.HTTP_410_GONE, "this link has been used up")

    artifact = await db.get(Artifact, link.artifact_id)
    if artifact is None or artifact.kind != ArtifactKind.set_archive or artifact.source_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "link not found")
    s = await db.get(DocumentSet, artifact.source_id)
    if s is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "link not found")
    owner = await db.get(User, s.owner_id)
    if owner is None or not owner.is_active:
        raise HTTPException(status.HTTP_410_GONE, "this share is no longer available")

    art, current = await svc.ensure_current_archive(db, background, s, requested_by=s.owner_id)
    if not svc.archive_is_ready(art, current):
        return JSONResponse(
            {"status": "building", "retry_after": 2}, status_code=status.HTTP_202_ACCEPTED
        )
    if art.item_count == 0:
        raise HTTPException(status.HTTP_410_GONE, "this share has no accessible content")

    link.download_count += 1
    link.last_downloaded_at = utcnow()
    await db.commit()
    return FileResponse(
        storage.set_archive_path(str(s.id)),
        media_type="application/zip",
        filename=_archive_filename(s),
    )
