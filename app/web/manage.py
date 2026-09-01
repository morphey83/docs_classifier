"""Web UI: domain settings + trash."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings as cfg
from app.db import get_session
from app.rbac import Cap
from app.services import trash as trash_svc
from app.web.csrf import CsrfGuard
from app.web.deps import DomainView, domain_by_slug, require_cap
from app.web.search import _tags_by_doc
from app.web.templating import render

router = APIRouter()


def _int(value: str, cap: int) -> int | None:
    try:
        return max(0, min(int(value), cap))
    except (TypeError, ValueError):
        return None


@router.get("/domains/{slug}/settings")
async def settings_page(
    request: Request, view: DomainView = Depends(domain_by_slug)
) -> Response:
    require_cap(view, Cap.manage)
    return render(
        request,
        "domain_settings.html",
        {
            "view": view,
            "s": view.domain.settings or {},
            "caps": {
                "quota": cfg.default_domain_quota_mb,
                "upload": cfg.max_upload_mb,
                "trash": cfg.default_trash_retention_days,
            },
            "is_owner": view.has(Cap.own),
        },
    )


@router.post("/domains/{slug}/settings")
async def settings_save(
    request: Request,
    name: str = Form(...),
    description: str = Form(default=""),
    default_ocr_lang: str = Form(default=""),
    archive_on_conflict: str = Form(default="skip"),
    allowed_types: str = Form(default=""),
    storage_quota_mb: str = Form(default=""),
    max_upload_mb: str = Form(default=""),
    trash_retention_days: str = Form(default=""),
    set_archive_ttl_days: str = Form(default=""),
    auto_ocr: str = Form(default=""),
    auto_index: str = Form(default=""),
    allow_public_links: str = Form(default=""),
    _: None = CsrfGuard,
    view: DomainView = Depends(domain_by_slug),
    db: AsyncSession = Depends(get_session),
) -> Response:
    require_cap(view, Cap.manage)
    d = view.domain
    d.name = name.strip() or d.name
    d.description = description.strip() or None

    s = dict(d.settings or {})
    s["auto_ocr"] = auto_ocr == "on"
    s["auto_index"] = auto_index == "on"
    s["allow_public_links"] = allow_public_links == "on"
    s["archive_on_conflict"] = "new" if archive_on_conflict == "new" else "skip"
    s["default_ocr_lang"] = default_ocr_lang.strip() or cfg.ocr_default_lang
    types = [t.strip().lower().lstrip(".") for t in allowed_types.split(",") if t.strip()]
    s["allowed_types"] = types or None
    for key, raw, cap in (
        ("storage_quota_mb", storage_quota_mb, cfg.default_domain_quota_mb),
        ("max_upload_mb", max_upload_mb, cfg.max_upload_mb),
        ("trash_retention_days", trash_retention_days, 3650),
        ("set_archive_ttl_days", set_archive_ttl_days, 3650),
    ):
        v = _int(raw, cap)
        if v is not None:
            s[key] = v
    d.settings = s
    await db.flush()
    return RedirectResponse(f"/domains/{d.slug}/settings", status_code=303)


@router.post("/domains/{slug}/delete")
async def domain_delete(
    _: None = CsrfGuard,
    view: DomainView = Depends(domain_by_slug),
    db: AsyncSession = Depends(get_session),
) -> Response:
    require_cap(view, Cap.own)
    await db.delete(view.domain)
    return RedirectResponse("/", status_code=303)


# --- trash -------------------------------------------------------
@router.get("/domains/{slug}/trash")
async def trash_page(
    request: Request,
    page: int = 1,
    view: DomainView = Depends(domain_by_slug),
    db: AsyncSession = Depends(get_session),
) -> Response:
    require_cap(view, Cap.delete)
    docs, total = await trash_svc.list_trash(db, view.domain.id, page=page, page_size=50)
    return render(
        request,
        "trash.html",
        {
            "view": view,
            "docs": docs,
            "tag_map": await _tags_by_doc(db, [d.id for d in docs]),
            "total": total,
            "is_owner": view.has(Cap.own),
        },
    )


@router.post("/domains/{slug}/trash/purge")
async def trash_purge(
    _: None = CsrfGuard,
    view: DomainView = Depends(domain_by_slug),
    db: AsyncSession = Depends(get_session),
) -> Response:
    require_cap(view, Cap.own)
    await trash_svc.purge_domain_trash(db, view.domain.id)
    return RedirectResponse(f"/domains/{view.domain.slug}/trash", status_code=303)
