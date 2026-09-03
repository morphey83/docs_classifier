"""Public gallery / slideshow for a set (``mode="gallery"`` share links).

Mounted at the site root. No auth cookie — the link token is the credential,
and rights are re-checked on every hit exactly like ``GET /d/{token}``:
the link must be live, the set must exist, its owner's account active, and
only ``is_public`` documents are ever exposed (§15).

    GET /g/{token}                 — the gallery page (grid + slideshow setup)
    GET /g/{token}.json            — machine-readable list (for external widgets)
    GET /g/{token}/feed            — Atom feed (new matches appear automatically)
    GET /g/{token}/slideshow       — standalone fullscreen slideshow
    GET /g/{token}/i/{doc_id}      — one document's bytes
    GET /g/{token}/i/{doc_id}/thumb
"""

from __future__ import annotations

import hashlib
import html
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.downloads import blob_download
from app.models import Artifact, ArtifactKind, DocumentSet, DownloadLink, User
from app.services import docsets as svc
from app.services import thumbs
from app.util import ratelimit
from app.util.time import as_aware, utcnow
from app.util.urls import absolute_url

router = APIRouter(tags=["gallery"])

_ORDERS = {
    "uploaded": lambda d: (as_aware(d.uploaded_at), str(d.id)),
    "doc_date": lambda d: (d.doc_date or datetime.min, str(d.id)),
    "title": lambda d: (d.title.lower(), str(d.id)),
    "size": lambda d: (d.size_bytes, str(d.id)),
}


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?")


async def _resolve(request: Request, token: str, db: AsyncSession):
    """(link, set, [public documents]) or an HTTPException."""
    if not ratelimit.check(f"g:{_client_ip(request)}", settings.public_download_rate_per_min):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "slow down")
    link = await db.scalar(select(DownloadLink).where(DownloadLink.token == token))
    if link is None or link.revoked_at is not None or link.mode != "gallery":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    if link.expires_at is not None and as_aware(link.expires_at) <= utcnow():
        raise HTTPException(status.HTTP_410_GONE, "this link has expired")
    artifact = await db.get(Artifact, link.artifact_id)
    if artifact is None or artifact.kind != ArtifactKind.set_archive or artifact.source_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    s = await db.get(DocumentSet, artifact.source_id)
    owner = await db.get(User, s.owner_id) if s else None
    if s is None or owner is None or not owner.is_active:
        raise HTTPException(status.HTTP_410_GONE, "this gallery is no longer available")
    docs = await svc.resolve_set(db, s, view="share")
    return link, s, docs


def _ordered(docs: list, order: str, seed: str | None) -> list:
    if order == "random":
        salt = (seed or "").encode()
        return sorted(docs, key=lambda d: hashlib.md5(salt + str(d.id).encode()).hexdigest())
    key = _ORDERS.get(order, _ORDERS["uploaded"])
    return sorted(docs, key=key)


def _is_image(doc) -> bool:
    return thumbs.can_thumb(doc.mime, doc.ext)


# --- JSON (for external widgets) -------------------------------------
@router.get("/g/{token}.json")
async def gallery_json(token: str, request: Request, db: AsyncSession = Depends(get_session)):
    _link, s, docs = await _resolve(request, token, db)
    base = absolute_url(f"/g/{token}")
    return {
        "set": {"name": s.name, "description": s.description},
        "count": len(docs),
        "documents": [
            {
                "id": str(d.id),
                "title": d.title,
                "doc_date": d.doc_date.isoformat() if d.doc_date else None,
                "size_bytes": d.size_bytes,
                "mime": d.mime,
                "is_image": _is_image(d),
                "url": f"{base}/i/{d.id}",
                "thumb": f"{base}/i/{d.id}/thumb" if _is_image(d) else None,
            }
            for d in _ordered(docs, "uploaded", None)
        ],
    }


# --- Atom feed ------------------------------------------------------
@router.get("/g/{token}/feed")
async def gallery_feed(token: str, request: Request, db: AsyncSession = Depends(get_session)):
    _link, s, docs = await _resolve(request, token, db)
    base = absolute_url(f"/g/{token}")
    docs = sorted(docs, key=lambda d: as_aware(d.uploaded_at), reverse=True)[:100]
    updated = (
        max((as_aware(d.uploaded_at) for d in docs), default=utcnow()).isoformat()
    )
    def _entry(d) -> str:
        item = f"{base}/i/{d.id}"
        enc = f'<link rel="enclosure" type="{d.mime}" href="{item}"/>' if _is_image(d) else ""
        return (
            f"<entry><title>{html.escape(d.title)}</title><id>{item}</id>"
            f"<updated>{as_aware(d.uploaded_at).isoformat()}</updated>"
            f'<link href="{item}"/>{enc}</entry>'
        )

    entries = "".join(_entry(d) for d in docs)
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        f"<title>{html.escape(s.name)}</title>"
        f"<id>{base}</id><updated>{updated}</updated>"
        f'<link href="{base}"/>{entries}</feed>'
    )
    return Response(body, media_type="application/atom+xml")


# --- per-document bytes -------------------------------------------
async def _doc_in_gallery(request: Request, token: str, doc_id: uuid.UUID, db: AsyncSession):
    _link, _s, docs = await _resolve(request, token, db)
    doc = next((d for d in docs if d.id == doc_id), None)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not in this gallery")
    return doc


@router.get("/g/{token}/i/{doc_id}")
async def gallery_item(
    token: str, doc_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_session)
) -> Response:
    return await blob_download(await _doc_in_gallery(request, token, doc_id, db))


@router.get("/g/{token}/i/{doc_id}/thumb")
async def gallery_thumb(
    token: str, doc_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_session)
) -> Response:
    doc = await _doc_in_gallery(request, token, doc_id, db)
    path = await thumbs.ensure_thumb(doc.sha256) if _is_image(doc) else None
    if path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no preview")
    return FileResponse(path, media_type="image/webp")


# --- HTML pages ---------------------------------------------------
def _templates():
    from app.web.templating import templates

    return templates


@router.get("/g/{token}")
async def gallery_page(
    token: str, request: Request, db: AsyncSession = Depends(get_session)
) -> Response:
    _link, s, docs = await _resolve(request, token, db)
    order = request.query_params.get("order") or "uploaded"
    docs = _ordered(docs, order, request.query_params.get("seed"))
    return _templates().TemplateResponse(
        request,
        "gallery.html",
        {
            "token": token,
            "set": s,
            "docs": docs,
            "image_count": sum(1 for d in docs if _is_image(d)),
            "order": order,
            "orders": [
                ("uploaded", "по загрузке"),
                ("doc_date", "по дате документа"),
                ("title", "по названию"),
                ("size", "по размеру"),
                ("random", "случайно"),
            ],
        },
    )


@router.get("/g/{token}/slideshow")
async def gallery_slideshow(
    token: str, request: Request, db: AsyncSession = Depends(get_session)
) -> Response:
    _link, s, docs = await _resolve(request, token, db)
    order = request.query_params.get("order") or "uploaded"
    seed = request.query_params.get("seed") or ""
    try:
        interval = max(2, min(120, int(request.query_params.get("interval") or 6)))
    except ValueError:
        interval = 6
    images = [d for d in _ordered(docs, order, seed) if _is_image(d)]
    if not images:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "в наборе нет изображений")
    return _templates().TemplateResponse(
        request,
        "slideshow.html",
        {"token": token, "set": s, "images": images, "interval": interval},
    )


# a bare /g/{token} with no trailing path but a manual "/slideshow" typo etc.
@router.get("/g/{token}/")
async def _slash(token: str) -> Response:
    return RedirectResponse(f"/g/{token}")
