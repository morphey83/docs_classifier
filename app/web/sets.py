"""Web UI: user-owned document sets — list, detail, filters, archive, links (§15)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import FileResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.db import get_session
from app.models import Artifact, ArtifactStatus, DocumentSetItem, User
from app.services import docsets as svc
from app.services.search import describe_filters
from app.util.time import as_aware, utcnow
from app.util.urls import absolute_url
from app.web.csrf import CsrfGuard
from app.web.deps import current_user
from app.web.templating import render

router = APIRouter()

_PREVIEW = 30


async def _load(db: AsyncSession, user: User, set_id: uuid.UUID):
    s = await svc.get_owned_set(db, set_id, user.id)
    if s is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "набор не найден")
    return s


@router.get("/sets")
async def sets_list(
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    sets = await svc.list_sets(db, user.id)
    counts = {}
    for s in sets:
        counts[s.id] = int(
            await db.scalar(
                select(func.count()).where(DocumentSetItem.set_id == s.id)
            )
            or 0
        )
    return render(request, "sets.html", {"sets": sets, "item_counts": counts})


@router.post("/sets")
async def sets_create(
    request: Request,
    name: str = Form(...),
    _: None = CsrfGuard,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    s = await svc.create_set(db, user, name=name, description=None)
    await db.commit()
    return RedirectResponse(f"/sets/{s.id}", status_code=303)


@router.get("/sets/{set_id}")
async def set_detail(
    request: Request,
    set_id: uuid.UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    s = await _load(db, user, set_id)
    filters = await svc.list_filters(db, s.id)
    resolved = await svc.resolve_set(db, s, view="full")
    explicit_ids = set(
        await db.scalars(
            select(DocumentSetItem.document_id).where(DocumentSetItem.set_id == s.id)
        )
    )
    public_n = sum(1 for d in resolved if d.is_public)
    links = [(link, absolute_url(f"/d/{link.token}")) for link in await svc.links_of_set(db, s.id)]

    export = None
    aid = request.query_params.get("export")
    if aid:
        art = await db.get(Artifact, uuid.UUID(aid)) if _is_uuid(aid) else None
        if art is not None and art.source_id == s.id and art.requested_by == user.id:
            export = art

    return render(
        request,
        "set.html",
        {
            "set": s,
            "filters": [(f, _filter_link(f)) for f in filters],
            "preview": resolved[:_PREVIEW],
            "resolved_count": len(resolved),
            "public_count": public_n,
            "explicit_ids": explicit_ids,
            "links": links,
            "export": export,
            "building": request.query_params.get("building"),
        },
    )


def _is_uuid(v: str) -> bool:
    try:
        uuid.UUID(v)
        return True
    except ValueError:
        return False


def _filter_link(f) -> str:
    """Rebuild a /search URL from a stored filter so the owner can eyeball it."""
    d = f.filter or {}
    parts: list[str] = []
    for key, qs in (("q", "q"), ("ext", "type"), ("status", "status")):
        if d.get(key):
            parts.append(f"{qs}={d[key]}")
    if d.get("tags_all"):
        parts.append("tags=" + ",".join(d["tags_all"]))
    if d.get("has_ocr") is True:
        parts.append("has_ocr=yes")
    if d.get("has_index") is True:
        parts.append("has_index=yes")
    if d.get("domain_ids") and len(d["domain_ids"]) == 1:
        parts.append(f"domain_id={d['domain_ids'][0]}")
    return "/search?" + "&".join(parts) if parts else "/search"


@router.post("/sets/{set_id}")
async def set_rename(
    request: Request,
    set_id: uuid.UUID,
    name: str = Form(...),
    description: str = Form(default=""),
    _: None = CsrfGuard,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    s = await _load(db, user, set_id)
    await svc.rename_set(db, s, name=name, description=description)
    return RedirectResponse(f"/sets/{s.id}", status_code=303)


@router.post("/sets/{set_id}/delete")
async def set_delete(
    request: Request,
    set_id: uuid.UUID,
    _: None = CsrfGuard,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    s = await _load(db, user, set_id)
    await svc.delete_set(db, s)
    return RedirectResponse("/sets", status_code=303)


@router.post("/sets/{set_id}/filters/{filter_id}/remove")
async def set_filter_remove(
    request: Request,
    set_id: uuid.UUID,
    filter_id: uuid.UUID,
    _: None = CsrfGuard,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    s = await _load(db, user, set_id)
    await svc.remove_filter(db, s, filter_id)
    return RedirectResponse(f"/sets/{s.id}", status_code=303)


@router.post("/sets/{set_id}/items/{document_id}/remove")
async def set_item_remove(
    request: Request,
    set_id: uuid.UUID,
    document_id: uuid.UUID,
    _: None = CsrfGuard,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    s = await _load(db, user, set_id)
    await svc.remove_item(db, s, document_id)
    return RedirectResponse(f"/sets/{s.id}", status_code=303)


@router.get("/sets/{set_id}/archive")
async def set_archive(
    request: Request,
    set_id: uuid.UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    s = await _load(db, user, set_id)
    artifact, current = await svc.ensure_current_archive(db, None, s, requested_by=user.id)
    if svc.archive_is_ready(artifact, current):
        return FileResponse(
            storage.set_archive_path(str(s.id)),
            media_type="application/zip",
            filename=f"{s.name}.zip",
        )
    return RedirectResponse(f"/sets/{s.id}?building=1", status_code=303)


@router.post("/sets/{set_id}/export")
async def set_full_export(
    request: Request,
    set_id: uuid.UUID,
    _: None = CsrfGuard,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    s = await _load(db, user, set_id)
    try:
        artifact = await svc.start_full_export(db, None, s, user)
    except svc.SetError as err:
        raise HTTPException(status.HTTP_409_CONFLICT, str(err)) from err
    return RedirectResponse(f"/sets/{s.id}?export={artifact.id}", status_code=303)


@router.get("/sets/{set_id}/export/{artifact_id}/download")
async def set_export_download(
    request: Request,
    set_id: uuid.UUID,
    artifact_id: uuid.UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    s = await _load(db, user, set_id)
    art = await db.get(Artifact, artifact_id)
    if art is None or art.source_id != s.id or art.requested_by != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "выгрузка не найдена")
    if art.status != ArtifactStatus.ready or not art.storage_key:
        return RedirectResponse(f"/sets/{s.id}?export={art.id}", status_code=303)
    if art.expires_at and as_aware(art.expires_at) <= utcnow():
        raise HTTPException(status.HTTP_410_GONE, "выгрузка устарела — запросите заново")
    path = storage.artifacts_dir() / art.storage_key
    if not path.is_file():
        raise HTTPException(status.HTTP_410_GONE, "файл выгрузки отсутствует")
    return FileResponse(path, media_type="application/zip", filename=f"{s.name} — полная.zip")


@router.post("/sets/{set_id}/links")
async def set_link_create(
    request: Request,
    set_id: uuid.UUID,
    kind: str = Form(...),
    _: None = CsrfGuard,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    s = await _load(db, user, set_id)
    await svc.create_share_link(
        db, None, s=s, user=user, kind="permanent" if kind == "permanent" else "one_time"
    )
    return RedirectResponse(f"/sets/{s.id}", status_code=303)


@router.post("/links/{link_id}/revoke")
async def link_revoke(
    link_id: uuid.UUID,
    _: None = CsrfGuard,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    from app.models import DocumentSet, DownloadLink

    link = await db.get(DownloadLink, link_id)
    artifact = await db.get(Artifact, link.artifact_id) if link else None
    s = await db.get(DocumentSet, artifact.source_id) if artifact else None
    if link is None or s is None or s.owner_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "ссылка не найдена")
    await svc.revoke_link(db, link)
    return RedirectResponse(f"/sets/{s.id}", status_code=303)


# used by web/search.py when saving the current query as a set filter
def filter_description(f, domain_names) -> str:
    return describe_filters(f, domain_names)
