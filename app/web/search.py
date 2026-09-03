"""Web UI: the one document search — root-level, with a domain filter,
card / table views, column sorting, and bulk actions on a selection
(full page + HTMX partial)."""

from __future__ import annotations

import uuid
from collections.abc import Mapping

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy import select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.jobs import dispatch
from app.models import (
    DocStatus,
    Document,
    DocumentTag,
    OcrStatus,
    Tag,
    User,
)
from app.ocr import engine as ocr_engine
from app.ocr.tasks import ocr_document
from app.rbac import ROLE_CAPS, Cap, Role
from app.services import docsets as docsets_svc
from app.services import domains as domains_svc
from app.services import search as search_svc
from app.services import tags as tags_svc
from app.services import trash as trash_svc
from app.services.search import describe_filters, index_document
from app.web.csrf import CsrfGuard
from app.web.deps import current_user
from app.web.templating import render

router = APIRouter()

PAGE_SIZE = 25
SORTS = {
    "uploaded_at": "загружен",
    "doc_date": "дата документа",
    "title": "название",
    "size": "размер",
}


def _csv(value: str | None) -> list[str]:
    return [s.strip() for s in value.split(",") if s.strip()] if value else []


def _tri(value: str | None) -> bool | None:
    if value in ("yes", "true", "1"):
        return True
    if value in ("no", "false", "0"):
        return False
    return None


def _status(value: str | None) -> DocStatus | None:
    try:
        return DocStatus(value) if value else None
    except ValueError:
        return None


def _filters_from_params(params: Mapping[str, str]) -> search_svc.SearchFilters:
    """The narrowing part of a /search query as a SearchFilters (no page/sort)."""
    return search_svc.SearchFilters(
        q=(params.get("q") or "") or None,
        tags_all=_csv(params.get("tags")),
        ext=(params.get("type") or "") or None,
        status=_status(params.get("status")),
        has_ocr=_tri(params.get("has_ocr")),
        has_index=_tri(params.get("has_index")),
    )


async def _distinct_exts(db: AsyncSession, domain_ids: list[uuid.UUID]) -> list[str]:
    """Extensions actually present in the caller's documents — feeds the filter."""
    if not domain_ids:
        return []
    rows = await db.scalars(
        select(Document.ext)
        .where(
            Document.domain_id.in_(domain_ids),
            Document.deleted_at.is_(None),
            Document.ext.is_not(None),
            Document.ext != "",
        )
        .distinct()
    )
    return sorted({e.lower() for e in rows})


async def _tags_by_doc(db: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, list[str]]:
    if not ids:
        return {}
    rows = await db.execute(
        select(DocumentTag.document_id, Tag.name)
        .join(Tag, Tag.id == DocumentTag.tag_id)
        .where(DocumentTag.document_id.in_(ids))
    )
    out: dict[uuid.UUID, list[str]] = {}
    for did, name in rows:
        out.setdefault(did, []).append(name)
    return out


def _scope(params: Mapping[str, str], dom_by_id: dict) -> tuple[uuid.UUID | None, list[uuid.UUID]]:
    raw = params.get("domain_id") or ""
    try:
        picked = uuid.UUID(raw) if raw else None
    except ValueError:
        picked = None
    if picked is not None and picked in dom_by_id:
        return picked, [picked]
    return None, list(dom_by_id)


PRESETS = ("active", "inbox", "trash")


async def _ctx(params: Mapping[str, str], db: AsyncSession, user: User) -> dict:
    memberships = await domains_svc.list_memberships(db, user)
    caps = {d.id: ROLE_CAPS[Role(m.role)] for d, m in memberships}
    dom_by_id = {d.id: d for d, _ in memberships}
    picked, scope_ids = _scope(params, dom_by_id)

    preset = params.get("preset") if params.get("preset") in PRESETS else "active"
    sort = params.get("sort") if params.get("sort") in SORTS else "uploaded_at"
    sort_dir = "asc" if params.get("dir") == "asc" else "desc"
    view = "table" if params.get("view") == "table" else "cards"

    f = _filters_from_params(params)
    if preset == "inbox":
        f.status = DocStatus.inbox
        f.exclude_deferred_by = user.id
    elif preset == "trash":
        f.only_trash = True
        f.status = None
    status_enum = f.status
    f.sort, f.sort_dir = sort, sort_dir
    f.page = max(1, int(params.get("page") or 1))
    f.page_size = PAGE_SIZE
    docs, total, _facets = await search_svc.search_documents(db, scope_ids, f)

    fd = {
        "q": params.get("q") or "",
        "tags": params.get("tags") or "",
        "type": params.get("type") or "",
        "status": status_enum.value if status_enum else "",
        "has_ocr": params.get("has_ocr") or "",
        "has_index": params.get("has_index") or "",
        "domain_id": str(picked) if picked else "",
        "preset": preset,
        "sort": sort,
        "dir": sort_dir,
    }
    return {
        "partial": "_results.html",
        "docs": docs,
        "tag_map": await _tags_by_doc(db, [d.id for d in docs]),
        "domain_names": {d.id: d.name for d, _ in memberships},
        "domain_slugs": {d.id: d.slug for d, _ in memberships},
        "doc_caps": {d.id: caps.get(d.domain_id, frozenset()) for d in docs},
        "total": total,
        "page": f.page,
        "pages": max(1, -(-total // PAGE_SIZE)),
        "ext_options": await _distinct_exts(db, list(dom_by_id)),
        "sorts": SORTS,
        "view": view,
        "preset": preset,
        "domains": [d for d, _ in memberships],
        "purge_domains": [
            dom_by_id[did] for did, c in caps.items() if Cap.own in c and did in dom_by_id
        ],
        "user_sets": await docsets_svc.list_sets(db, user.id),
        "can_publish_any": any(Cap.manage in c for c in caps.values()),
        # everything that narrows the result set (not page / sort / view) —
        # the client wipes its selection when this string changes.
        "filter_sig": "|".join(
            fd[k]
            for k in ("preset", "domain_id", "q", "tags", "type", "status", "has_ocr", "has_index")
        ),
        "f": fd,
    }


@router.get("/search")
async def search(
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    return render(request, "search.html", await _ctx(request.query_params, db, user))


@router.post("/search/bulk")
async def search_bulk(
    request: Request,
    _: None = CsrfGuard,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    form = await request.form()
    action = form.get("action") or ""
    ids: list[uuid.UUID] = []
    for raw in (form.get("doc_ids") or "").split(","):
        try:
            ids.append(uuid.UUID(raw.strip()))
        except ValueError:
            continue

    memberships = await domains_svc.list_memberships(db, user)
    dom_by_id = {d.id: d for d, _ in memberships}
    caps = {d.id: ROLE_CAPS[Role(m.role)] for d, m in memberships}
    # trash actions operate on deleted rows, so don't filter them out here
    trash_scope = action in ("restore", "purge")
    docs = list(
        await db.scalars(
            select(Document).where(
                Document.id.in_(ids),
                true() if trash_scope else Document.deleted_at.is_(None),
            )
        )
    ) if ids else []
    docs = [d for d in docs if d.domain_id in caps]

    msg = await _apply_bulk(db, user, action, docs, caps, dom_by_id, form)
    await db.commit()

    ctx = await _ctx(form, db, user)
    return render(request, "_results.html", ctx, toast=msg)


async def _apply_bulk(db, user, action, docs, caps, dom_by_id, form) -> str:
    if action == "save_filter":
        return await _save_filter(db, user, form)
    if not docs:
        return "Ничего не выбрано."

    if action == "index":
        n = 0
        for d in docs:
            if Cap.process in caps[d.domain_id]:
                await index_document(db, d)
                n += 1
        return f"Проиндексировано: {n}"

    if action == "ocr":
        n = skipped = 0
        for d in docs:
            if Cap.process not in caps[d.domain_id]:
                continue
            if not ocr_engine.is_supported(d.mime):
                skipped += 1
                continue
            d.ocr_status = OcrStatus.pending
            await db.flush()
            await dispatch(None, "ocr_document", ocr_document, document_id=d.id)
            n += 1
        tail = f", пропущено (тип не поддерживается): {skipped}" if skipped else ""
        return f"Отправлено на распознавание: {n}{tail}"

    if action == "tags":
        names = _csv(form.get("tag_names"))
        if not names:
            return "Укажите теги."
        writable = [d.id for d in docs if Cap.write in caps[d.domain_id]]
        skipped = len(docs) - len(writable)
        tag_ids = await tags_svc.resolve_names(db, names, actor=user)
        n = await tags_svc.add_tags_to_documents(db, writable, tag_ids, actor=user)
        tail = f", пропущено (нет прав): {skipped}" if skipped else ""
        return f"Проставлено тегов: {n}{tail}"

    if action == "restore":
        n = 0
        for d in docs:
            if Cap.delete in caps[d.domain_id] and d.deleted_at is not None:
                try:
                    await trash_svc.restore(db, d)
                    n += 1
                except trash_svc.TrashError:
                    pass
        return f"Восстановлено: {n}"

    if action == "purge":
        n = 0
        for d in docs:
            if Cap.delete in caps[d.domain_id] and d.deleted_at is not None:
                await trash_svc.hard_purge(db, d)
                n += 1
        return f"Удалено навсегда: {n}"

    if action in ("public", "private"):
        want = action == "public"
        n = skipped = 0
        for d in docs:
            if Cap.manage not in caps[d.domain_id]:
                skipped += 1
                continue
            if d.is_public != want:
                d.is_public = want
            n += 1
        await db.flush()
        tail = f", пропущено (нет прав): {skipped}" if skipped else ""
        return f"{'Опубликовано' if want else 'Сделано приватным'}: {n}{tail}"

    if action == "set":
        new_name = (form.get("new_name") or "").strip()
        set_id = form.get("set_id") or ""
        if set_id == "__new__" or (not set_id and new_name):
            s = await docsets_svc.create_set(
                db, user, name=new_name or "Новый набор", document_ids=[d.id for d in docs]
            )
            return f"Создан набор «{s.name}»"
        try:
            s = await docsets_svc.get_owned_set(db, uuid.UUID(set_id), user.id)
        except ValueError:
            s = None
        if s is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "набор не найден")
        added = await docsets_svc.add_items(db, s, [d.id for d in docs], actor=user)
        return f"Добавлено в «{s.name}»: {added}"

    return "Неизвестное действие."


async def _save_filter(db, user, form) -> str:
    memberships = await domains_svc.list_memberships(db, user)
    dom_by_id_all = {d.id: d for d, _ in memberships}
    picked, _scope_ids = _scope(form, dom_by_id_all)
    f = _filters_from_params(form)
    if picked is not None:
        f.domain_ids = [picked]
    desc = describe_filters(f, {d.id: d.name for d, _ in memberships})
    new_name = (form.get("new_name") or "").strip()
    set_id = form.get("set_id") or ""
    if set_id == "__new__" or (not set_id and new_name):
        s = await docsets_svc.create_set(db, user, name=new_name or "Новый набор")
    else:
        try:
            s = await docsets_svc.get_owned_set(db, uuid.UUID(set_id), user.id)
        except ValueError:
            s = None
        if s is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "набор не найден")
    await docsets_svc.add_filter(db, s, f, description=desc)
    return f"Фильтр сохранён в «{s.name}»"
