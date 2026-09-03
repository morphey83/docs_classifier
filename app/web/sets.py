"""Web UI: user-owned document sets — list, detail, filters, archive, links (§15)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import FileResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.db import get_session
from app.models import Artifact, ArtifactStatus, User
from app.services import docsets as svc
from app.services.search import describe_filters
from app.util.time import as_aware, utcnow
from app.util.urls import absolute_url
from app.web.csrf import CsrfGuard
from app.web.deps import current_user
from app.web.templating import render

router = APIRouter()

_SETS_PER_PAGE = 15
_FILTERS_PER_PAGE = 10
_DOCS_PER_PAGE = 15


async def _load(db: AsyncSession, user: User, set_id: uuid.UUID):
    s = await svc.get_owned_set(db, set_id, user.id)
    if s is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "набор не найден")
    return s


def _int(raw: str | None, default: int = 1) -> int:
    try:
        return max(1, int(raw or default))
    except (TypeError, ValueError):
        return default


@router.get("/sets")
async def sets_list(
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    q = (request.query_params.get("q") or "").strip()
    all_sets = await svc.list_sets(db, user.id, q=q)
    total = len(all_sets)
    pages = max(1, -(-total // _SETS_PER_PAGE))
    page = min(_int(request.query_params.get("page")), pages)
    window = all_sets[(page - 1) * _SETS_PER_PAGE : page * _SETS_PER_PAGE]

    rows = []
    for s in window:
        public, accessible = await svc.set_doc_counts(db, s)
        rows.append(
            {
                "set": s,
                "n_filters": await svc.count_filters(db, s.id),
                "n_explicit": await svc.count_items(db, s.id),
                "public": public,
                "accessible": accessible,
            }
        )
    return render(
        request,
        "sets.html",
        {
            "partial": "_sets_table.html",
            "rows": rows,
            "q": q,
            "page": page,
            "pages": pages,
            "total": total,
        },
    )


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


async def _detail_ctx(request: Request, db: AsyncSession, user: User, s, *, export_id=None) -> dict:
    all_filters = await svc.list_filters(db, s.id)
    fpages = max(1, -(-len(all_filters) // _FILTERS_PER_PAGE))
    fpage = min(_int(request.query_params.get("fpage")), fpages)
    filter_rows = []
    for fr in all_filters[(fpage - 1) * _FILTERS_PER_PAGE : fpage * _FILTERS_PER_PAGE]:
        public, total = await svc.filter_doc_counts(db, s, fr)
        filter_rows.append(
            {"f": fr, "link": _filter_link(fr), "public": public, "total": total}
        )

    item_total = await svc.count_items(db, s.id)
    dpages = max(1, -(-item_total // _DOCS_PER_PAGE))
    dpage = min(_int(request.query_params.get("dpage")), dpages)
    items = await svc.list_items(
        db, s.id, offset=(dpage - 1) * _DOCS_PER_PAGE, limit=_DOCS_PER_PAGE
    )

    resolved = await svc.resolve_set(db, s, view="full")
    links = [
        (link, absolute_url(f"/{'g' if link.mode == 'gallery' else 'd'}/{link.token}"))
        for link in await svc.links_of_set(db, s.id)
    ]

    export = None
    aid = export_id or request.query_params.get("export")
    if aid and _is_uuid(str(aid)):
        art = await db.get(Artifact, uuid.UUID(str(aid)))
        if art is not None and art.source_id == s.id and art.requested_by == user.id:
            export = art

    return {
        "partial": "_set_body.html",
        "set": s,
        "filter_rows": filter_rows,
        "fpage": fpage,
        "fpages": fpages,
        "filter_total": len(all_filters),
        "items": items,
        "dpage": dpage,
        "dpages": dpages,
        "item_total": item_total,
        "resolved_count": len(resolved),
        "public_count": sum(1 for d in resolved if d.is_public),
        "links": links,
        "export": export,
        "building": request.query_params.get("building"),
    }


async def _set_response(request, db, user, s, *, toast=None, export_id=None) -> Response:
    ctx = await _detail_ctx(request, db, user, s, export_id=export_id)
    if request.headers.get("HX-Request"):
        return render(request, "set.html", ctx, toast=toast)
    return RedirectResponse(f"/sets/{s.id}", status_code=303)


@router.get("/sets/{set_id}")
async def set_detail(
    request: Request,
    set_id: uuid.UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    s = await _load(db, user, set_id)
    return render(request, "set.html", await _detail_ctx(request, db, user, s))


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
    if d.get("q"):
        parts.append(f"q={d['q']}")
    if d.get("status"):
        parts.append(f"status={d['status']}")
    for t in d.get("types", []):
        parts.append(f"type={t}")
    if d.get("ext"):
        parts.append(f"type={d['ext']}")
    toks = list(d.get("tags_all") or []) + ["-" + t for t in (d.get("tags_none") or [])]
    if toks:
        parts.append("tags=" + ",".join(toks))
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
    return await _set_response(request, db, user, s, toast="Сохранено")


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
    if request.headers.get("HX-Request"):
        r = Response(status_code=204)
        r.headers["HX-Redirect"] = "/sets"
        return r
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
    return await _set_response(request, db, user, s)


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
    return await _set_response(request, db, user, s)


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
    return await _set_response(request, db, user, s, export_id=artifact.id)


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
    kind: str = Form(default="one_time"),
    mode: str = Form(default="archive"),
    _: None = CsrfGuard,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    s = await _load(db, user, set_id)
    await svc.create_share_link(
        db, None, s=s, user=user,
        kind="permanent" if kind == "permanent" else "one_time",
        mode="gallery" if mode == "gallery" else "archive",
    )
    return await _set_response(request, db, user, s, toast="Ссылка создана")


@router.post("/links/{link_id}/revoke")
async def link_revoke(
    request: Request,
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
    return await _set_response(request, db, user, s, toast="Ссылка отозвана")


# used by web/search.py when saving the current query as a set filter
def filter_description(f, domain_names) -> str:
    return describe_filters(f, domain_names)
