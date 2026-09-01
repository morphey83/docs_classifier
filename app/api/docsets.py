"""Document sets, their archive cache, and public share links (§15)."""

from __future__ import annotations

import secrets
import uuid
from datetime import timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.api._common import document_out
from app.config import settings
from app.db import get_session
from app.deps import DomainCtx, require
from app.jobs import dispatch
from app.models import (
    Artifact,
    ArtifactKind,
    ArtifactStatus,
    Document,
    DocumentSet,
    DocumentSetItem,
    Domain,
    DownloadLink,
    SetVisibility,
    User,
)
from app.rbac import ROLE_CAPS, Cap, Role
from app.schemas.docsets import (
    ArchiveStatusOut,
    LinkCreate,
    LinkOut,
    SetCreate,
    SetDetail,
    SetItemOut,
    SetItemsAdd,
    SetOut,
    SetUpdate,
)
from app.security import get_current_user
from app.services import docsets as svc
from app.services import domains as domains_svc
from app.util import ratelimit
from app.util.time import as_aware, utcnow
from app.util.urls import absolute_url

router = APIRouter(tags=["sets"])


# --- helpers ----------------------------------------------------------
def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "?"


def _has_manage(role: Role | str) -> bool:
    return Cap.manage in ROLE_CAPS[Role(role)]


def _has_download(role: Role | str) -> bool:
    return Cap.download in ROLE_CAPS[Role(role)]


async def _load_set(
    db: AsyncSession, ctx: DomainCtx, set_id: uuid.UUID, *, need_edit: bool = False
) -> DocumentSet:
    s = await db.get(DocumentSet, set_id)
    if s is None or s.domain_id != ctx.domain.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "set not found")
    is_creator = s.created_by == ctx.user.id
    if not is_creator and s.visibility != SetVisibility.domain:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "set not found")
    if need_edit and not is_creator and not ctx.has(Cap.manage):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "editing this set needs 'manage' in the domain"
        )
    return s


def _archive_filename(set_obj: DocumentSet) -> str:
    safe = "".join(c if c.isalnum() or c in " -_." else "_" for c in set_obj.name).strip()
    return f"{safe or 'set'} {utcnow():%Y-%m-%d}.zip"


async def _set_out(db: AsyncSession, set_obj: DocumentSet) -> SetOut:
    # server_default / onupdate columns (created_at, updated_at) may be expired
    # after a flush — reload before the sync pydantic validation touches them.
    await db.refresh(set_obj)
    return SetOut.model_validate(set_obj)


async def _set_detail(db: AsyncSession, set_obj: DocumentSet) -> SetDetail:
    rows = await db.execute(
        select(Document, DocumentSetItem.added_at, DocumentSetItem.position)
        .join(DocumentSetItem, DocumentSetItem.document_id == Document.id)
        .where(DocumentSetItem.set_id == set_obj.id)
        .order_by(DocumentSetItem.position, DocumentSetItem.added_at)
    )
    items = [
        SetItemOut(document=await document_out(db, doc), added_at=added_at, position=pos)
        for doc, added_at, pos in rows
    ]
    base = await _set_out(db, set_obj)
    return SetDetail(**base.model_dump(), items=items)


async def _ensure_current(
    db: AsyncSession,
    background: BackgroundTasks | None,
    domain: Domain,
    set_obj: DocumentSet,
    *,
    requested_by: uuid.UUID | None,
) -> tuple[Artifact, str]:
    """Compare the live set hash to the cached artifact; queue a rebuild if stale."""
    docs = await svc.set_documents(db, set_obj.id)
    tags = await svc.tags_by_doc(db, [d.id for d in docs])
    current = svc.set_content_hash(docs, tags)

    artifact = await svc.get_set_artifact(db, set_obj.id)
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
        await dispatch(
            background, "build_set_archive", svc.build_set_archive, set_id=set_obj.id
        )
    return artifact, current


# --- set CRUD --------------------------------------------------------
@router.post(
    "/domains/{domain_id}/sets", response_model=SetOut, status_code=status.HTTP_201_CREATED
)
async def create_set(
    body: SetCreate,
    ctx: DomainCtx = Depends(require(Cap.view)),
    db: AsyncSession = Depends(get_session),
) -> SetOut:
    s = await svc.create_set(
        db,
        ctx.domain,
        ctx.user,
        name=body.name,
        description=body.description,
        visibility=body.visibility,
        document_ids=body.document_ids,
    )
    return await _set_out(db, s)


@router.get("/domains/{domain_id}/sets", response_model=list[SetOut])
async def list_sets(
    ctx: DomainCtx = Depends(require(Cap.view)), db: AsyncSession = Depends(get_session)
) -> list[SetOut]:
    return [SetOut.model_validate(s) for s in await svc.list_sets(db, ctx.domain.id, ctx.user.id)]


@router.get("/domains/{domain_id}/sets/{set_id}", response_model=SetDetail)
async def get_set(
    set_id: uuid.UUID,
    ctx: DomainCtx = Depends(require(Cap.view)),
    db: AsyncSession = Depends(get_session),
) -> SetDetail:
    return await _set_detail(db, await _load_set(db, ctx, set_id))


@router.patch("/domains/{domain_id}/sets/{set_id}", response_model=SetOut)
async def update_set(
    set_id: uuid.UUID,
    body: SetUpdate,
    ctx: DomainCtx = Depends(require(Cap.view)),
    db: AsyncSession = Depends(get_session),
) -> SetOut:
    s = await _load_set(db, ctx, set_id, need_edit=True)
    if body.name is not None:
        s.name = body.name.strip()
    if body.description is not None:
        s.description = body.description or None
    if body.visibility is not None:
        s.visibility = body.visibility
    await db.flush()
    return await _set_out(db, s)


@router.delete("/domains/{domain_id}/sets/{set_id}")
async def delete_set(
    set_id: uuid.UUID,
    ctx: DomainCtx = Depends(require(Cap.view)),
    db: AsyncSession = Depends(get_session),
) -> Response:
    s = await _load_set(db, ctx, set_id, need_edit=True)
    artifact = await svc.get_set_artifact(db, set_id)
    if artifact is not None:
        storage.set_archive_path(str(set_id)).unlink(missing_ok=True)
        await db.delete(artifact)  # cascades its download_links
    await db.delete(s)  # cascades document_set_item
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- items ----------------------------------------------------------
@router.post("/domains/{domain_id}/sets/{set_id}/items", response_model=SetDetail)
async def add_items(
    set_id: uuid.UUID,
    body: SetItemsAdd,
    ctx: DomainCtx = Depends(require(Cap.view)),
    db: AsyncSession = Depends(get_session),
) -> SetDetail:
    s = await _load_set(db, ctx, set_id, need_edit=True)
    await svc.add_items(db, s, body.document_ids, actor=ctx.user)
    return await _set_detail(db, s)


@router.delete("/domains/{domain_id}/sets/{set_id}/items/{document_id}", response_model=SetDetail)
async def remove_item(
    set_id: uuid.UUID,
    document_id: uuid.UUID,
    ctx: DomainCtx = Depends(require(Cap.view)),
    db: AsyncSession = Depends(get_session),
) -> SetDetail:
    s = await _load_set(db, ctx, set_id, need_edit=True)
    await svc.remove_item(db, s, document_id)
    return await _set_detail(db, s)


# --- archive ------------------------------------------------------
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


@router.get("/domains/{domain_id}/sets/{set_id}/archive", response_model=ArchiveStatusOut)
async def archive_status(
    set_id: uuid.UUID,
    background: BackgroundTasks,
    ctx: DomainCtx = Depends(require(Cap.download)),
    db: AsyncSession = Depends(get_session),
) -> ArchiveStatusOut:
    s = await _load_set(db, ctx, set_id)
    artifact, current = await _ensure_current(
        db, background, ctx.domain, s, requested_by=ctx.user.id
    )
    return _archive_status(artifact, current)


@router.get("/domains/{domain_id}/sets/{set_id}/archive/download")
async def download_archive(
    set_id: uuid.UUID,
    background: BackgroundTasks,
    ctx: DomainCtx = Depends(require(Cap.download)),
    db: AsyncSession = Depends(get_session),
) -> Response:
    s = await _load_set(db, ctx, set_id)
    artifact, current = await _ensure_current(
        db, background, ctx.domain, s, requested_by=ctx.user.id
    )
    path = storage.set_archive_path(str(set_id))
    if (
        artifact.status == ArtifactStatus.ready
        and artifact.content_hash == current
        and path.is_file()
    ):
        return FileResponse(
            path, media_type="application/zip", filename=_archive_filename(s)
        )
    return JSONResponse(
        {"status": "building", "retry_after": 2}, status_code=status.HTTP_202_ACCEPTED
    )


# --- share links --------------------------------------------------
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


@router.post(
    "/domains/{domain_id}/sets/{set_id}/links",
    response_model=LinkOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_link(
    set_id: uuid.UUID,
    body: LinkCreate,
    background: BackgroundTasks,
    ctx: DomainCtx = Depends(require(Cap.view)),
    db: AsyncSession = Depends(get_session),
) -> LinkOut:
    s = await _load_set(db, ctx, set_id)
    if not (ctx.domain.settings or {}).get("allow_public_links", True):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "public links are disabled for this domain")
    if body.kind == "permanent" and not ctx.has(Cap.write):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "a permanent link needs 'write'")
    if body.kind == "one_time" and not ctx.has(Cap.download):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "a link needs 'download'")
    if body.expires_at is not None and as_aware(body.expires_at) <= utcnow():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "expires_at is in the past")

    artifact, _ = await _ensure_current(db, background, ctx.domain, s, requested_by=ctx.user.id)
    link = DownloadLink(
        artifact_id=artifact.id,
        token=secrets.token_urlsafe(24),
        max_downloads=1 if body.kind == "one_time" else None,
        expires_at=body.expires_at,
        created_by=ctx.user.id,
    )
    db.add(link)
    await db.flush()
    return _link_out(link)


@router.get("/domains/{domain_id}/sets/{set_id}/links", response_model=list[LinkOut])
async def list_links(
    set_id: uuid.UUID,
    ctx: DomainCtx = Depends(require(Cap.view)),
    db: AsyncSession = Depends(get_session),
) -> list[LinkOut]:
    s = await _load_set(db, ctx, set_id)
    artifact = await svc.get_set_artifact(db, s.id)
    if artifact is None:
        return []
    rows = await db.scalars(
        select(DownloadLink)
        .where(DownloadLink.artifact_id == artifact.id)
        .order_by(DownloadLink.created_at.desc())
    )
    return [_link_out(link) for link in rows]


@router.delete("/links/{link_id}")
async def revoke_link(
    link_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    link = await db.get(DownloadLink, link_id)
    if link is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "link not found")
    artifact = await db.get(Artifact, link.artifact_id)
    row = await domains_svc.get_membership(db, artifact.domain_id, user.id) if artifact else None
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "link not found")
    if link.created_by != user.id and not _has_manage(row[1].role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "only the creator or 'manage' can revoke")
    if link.revoked_at is None:
        link.revoked_at = utcnow()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- public download (no auth) -----------------------------------
@router.get("/d/{token}")
async def public_download(
    token: str,
    request: Request,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_session),
) -> Response:
    ip = _client_ip(request)
    if not ratelimit.check(f"d:{ip}", settings.public_download_rate_per_min):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "slow down")

    link = await db.scalar(select(DownloadLink).where(DownloadLink.token == token))
    if link is None or link.revoked_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "link not found")
    if link.expires_at is not None and as_aware(link.expires_at) <= utcnow():
        raise HTTPException(status.HTTP_410_GONE, "this link has expired")
    if link.max_downloads is not None and link.download_count >= link.max_downloads:
        raise HTTPException(status.HTTP_410_GONE, "this link has been used up")

    artifact = await db.get(Artifact, link.artifact_id)
    if artifact is None or artifact.kind != ArtifactKind.set_archive or artifact.source_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "link not found")
    set_obj = await db.get(DocumentSet, artifact.source_id)
    domain = await db.get(Domain, artifact.domain_id)
    if set_obj is None or domain is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "link not found")

    if not (domain.settings or {}).get("allow_public_links", True):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "public links are disabled")
    row = (
        await domains_svc.get_membership(db, domain.id, link.created_by)
        if link.created_by
        else None
    )
    if row is None or not _has_download(row[1].role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "this link is no longer valid")

    art, current = await _ensure_current(
        db, background, domain, set_obj, requested_by=link.created_by
    )
    path = storage.set_archive_path(str(set_obj.id))
    if (
        art.status == ArtifactStatus.ready
        and art.content_hash == current
        and path.is_file()
    ):
        link.download_count += 1
        link.last_downloaded_at = utcnow()
        await db.commit()
        return FileResponse(
            path, media_type="application/zip", filename=_archive_filename(set_obj)
        )
    return JSONResponse(
        {"status": "building", "retry_after": 2}, status_code=status.HTTP_202_ACCEPTED
    )
