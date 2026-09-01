"""Web UI: document sets — list, detail, items, archive, share links."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import FileResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.db import get_session
from app.models import DocumentSet, DownloadLink, SetVisibility, User
from app.rbac import Cap
from app.services import docsets as svc
from app.util.urls import absolute_url
from app.web.csrf import CsrfGuard
from app.web.deps import DomainView, current_user, domain_by_slug
from app.web.search import _tags_by_doc
from app.web.templating import render

router = APIRouter()


async def _load_set(
    db: AsyncSession, view: DomainView, user: User, set_id: uuid.UUID
) -> DocumentSet:
    s = await db.get(DocumentSet, set_id)
    if s is None or s.domain_id != view.domain.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "набор не найден")
    if s.created_by != user.id and s.visibility != SetVisibility.domain:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "набор не найден")
    return s


def _can_edit(s: DocumentSet, view: DomainView, user: User) -> bool:
    return s.created_by == user.id or view.has(Cap.manage)


@router.get("/domains/{slug}/sets")
async def sets_list(
    request: Request,
    view: DomainView = Depends(domain_by_slug),
    db: AsyncSession = Depends(get_session),
) -> Response:
    user = request.state.user
    sets = await svc.list_sets(db, view.domain.id, user.id)
    return render(request, "sets.html", {"view": view, "sets": sets})


@router.post("/domains/{slug}/sets")
async def sets_create(
    request: Request,
    name: str = Form(...),
    visibility: str = Form(default="private"),
    _: None = CsrfGuard,
    view: DomainView = Depends(domain_by_slug),
    db: AsyncSession = Depends(get_session),
) -> Response:
    vis = SetVisibility.domain if visibility == "domain" else SetVisibility.private
    s = await svc.create_set(
        db, view.domain, request.state.user, name=name, description=None, visibility=vis
    )
    await db.flush()
    return RedirectResponse(f"/domains/{view.domain.slug}/sets/{s.id}", status_code=303)


@router.get("/domains/{slug}/sets/{set_id}")
async def set_detail(
    request: Request,
    set_id: uuid.UUID,
    view: DomainView = Depends(domain_by_slug),
    db: AsyncSession = Depends(get_session),
) -> Response:
    user = request.state.user
    s = await _load_set(db, view, user, set_id)
    docs = await svc.set_documents(db, s.id)
    tag_map = await _tags_by_doc(db, [d.id for d in docs])
    artifact = await svc.get_set_artifact(db, s.id)
    links = []
    if artifact is not None:
        links = list(
            await db.scalars(
                select(DownloadLink)
                .where(DownloadLink.artifact_id == artifact.id, DownloadLink.revoked_at.is_(None))
                .order_by(DownloadLink.created_at.desc())
            )
        )
    return render(
        request,
        "set.html",
        {
            "view": view,
            "set": s,
            "docs": docs,
            "tag_map": tag_map,
            "links": [(link, absolute_url(f"/d/{link.token}")) for link in links],
            "can_edit": _can_edit(s, view, user),
        },
    )


@router.post("/domains/{slug}/sets/{set_id}/items")
async def set_add_item(
    request: Request,
    set_id: uuid.UUID,
    document_id: uuid.UUID = Form(...),
    back: str = Form(default=""),
    _: None = CsrfGuard,
    view: DomainView = Depends(domain_by_slug),
    db: AsyncSession = Depends(get_session),
) -> Response:
    user = request.state.user
    s = await _load_set(db, view, user, set_id)
    if not _can_edit(s, view, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "нельзя редактировать этот набор")
    await svc.add_items(db, s, [document_id], actor=user)
    target = back if back.startswith("/") else f"/domains/{view.domain.slug}/sets/{s.id}"
    return RedirectResponse(target, status_code=303)


@router.post("/domains/{slug}/sets/{set_id}/items/{document_id}/remove")
async def set_remove_item(
    request: Request,
    set_id: uuid.UUID,
    document_id: uuid.UUID,
    _: None = CsrfGuard,
    view: DomainView = Depends(domain_by_slug),
    db: AsyncSession = Depends(get_session),
) -> Response:
    user = request.state.user
    s = await _load_set(db, view, user, set_id)
    if not _can_edit(s, view, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "нельзя редактировать этот набор")
    await svc.remove_item(db, s, document_id)
    return RedirectResponse(f"/domains/{view.domain.slug}/sets/{s.id}", status_code=303)


@router.post("/domains/{slug}/sets/{set_id}/delete")
async def set_delete(
    request: Request,
    set_id: uuid.UUID,
    _: None = CsrfGuard,
    view: DomainView = Depends(domain_by_slug),
    db: AsyncSession = Depends(get_session),
) -> Response:
    user = request.state.user
    s = await _load_set(db, view, user, set_id)
    if not _can_edit(s, view, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "нельзя удалить этот набор")
    artifact = await svc.get_set_artifact(db, s.id)
    if artifact is not None:
        storage.set_archive_path(str(s.id)).unlink(missing_ok=True)
        await db.delete(artifact)
    await db.delete(s)
    return RedirectResponse(f"/domains/{view.domain.slug}/sets", status_code=303)


@router.get("/domains/{slug}/sets/{set_id}/archive")
async def set_archive(
    request: Request,
    set_id: uuid.UUID,
    view: DomainView = Depends(domain_by_slug),
    db: AsyncSession = Depends(get_session),
) -> Response:
    user = request.state.user
    s = await _load_set(db, view, user, set_id)
    if not view.has(Cap.download):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "нужно право download")
    artifact, current = await svc.ensure_current_archive(
        db, None, view.domain, s, requested_by=user.id
    )
    path = storage.set_archive_path(str(s.id))
    if svc.archive_is_ready(artifact, current):
        return FileResponse(path, media_type="application/zip", filename=f"{s.name}.zip")
    return RedirectResponse(
        f"/domains/{view.domain.slug}/sets/{s.id}?building=1", status_code=303
    )


@router.post("/domains/{slug}/sets/{set_id}/links")
async def set_link(
    request: Request,
    set_id: uuid.UUID,
    kind: str = Form(...),
    _: None = CsrfGuard,
    view: DomainView = Depends(domain_by_slug),
    db: AsyncSession = Depends(get_session),
) -> Response:
    user = request.state.user
    s = await _load_set(db, view, user, set_id)
    try:
        await svc.create_share_link(
            db, None, domain=view.domain, set_obj=s, user=user, role=view.role,
            kind="permanent" if kind == "permanent" else "one_time",
        )
    except svc.SetError as err:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(err)) from err
    return RedirectResponse(f"/domains/{view.domain.slug}/sets/{s.id}", status_code=303)


@router.post("/links/{link_id}/revoke")
async def link_revoke(
    link_id: uuid.UUID,
    _: None = CsrfGuard,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    from app.models import Artifact
    from app.rbac import ROLE_CAPS, Role
    from app.services import domains as domains_svc
    from app.util.time import utcnow

    link = await db.get(DownloadLink, link_id)
    artifact = await db.get(Artifact, link.artifact_id) if link else None
    row = await domains_svc.get_membership(db, artifact.domain_id, user.id) if artifact else None
    if link is None or row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "ссылка не найдена")
    if link.created_by != user.id and Cap.manage not in ROLE_CAPS[Role(row[1].role)]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "только создатель ссылки или 'manage'")
    if link.revoked_at is None:
        link.revoked_at = utcnow()
    return RedirectResponse(
        f"/domains/{row[0].slug}/sets/{artifact.source_id}", status_code=303
    )
