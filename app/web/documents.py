"""Web UI: upload, document detail, inline edits (tags / title / OCR / index)."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.db import get_session
from app.ingest import archive
from app.jobs import dispatch
from app.models import (
    BatchKind,
    DocumentSet,
    DocumentTag,
    OcrStatus,
    SetVisibility,
    Tag,
    UploadBatch,
    UploadBatchItem,
    User,
)
from app.ocr import engine as ocr_engine
from app.ocr.tasks import ocr_document
from app.rbac import Cap
from app.services import docsets as docsets_svc
from app.services import documents as docs_svc
from app.services import domains as domains_svc
from app.services import tags as tags_svc
from app.services import trash as trash_svc
from app.services.ingest import process_archive
from app.services.search import index_document
from app.web.csrf import CsrfGuard
from app.web.deps import current_user, load_document, require_cap
from app.web.templating import render

router = APIRouter()


# --- upload (root-level, pick the target domain) --------------------
async def _uploadable_domains(db: AsyncSession, user: User):
    out = []
    for domain, member in await domains_svc.list_memberships(db, user):
        from app.rbac import ROLE_CAPS, Role

        if Cap.upload in ROLE_CAPS[Role(member.role)]:
            out.append(domain)
    return out


@router.get("/upload")
async def upload_form(
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    doms = await _uploadable_domains(db, user)
    picked = request.query_params.get("domain") or (doms[0].slug if doms else "")
    return render(request, "upload.html", {"domains": doms, "picked": picked})


@router.post("/upload")
async def upload_submit(
    request: Request,
    file: UploadFile,
    domain: str = Form(...),
    on_conflict: str | None = Form(default=None),
    _: None = CsrfGuard,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    doms = await _uploadable_domains(db, user)
    target = next((d for d in doms if d.slug == domain or str(d.id) == domain), None)
    if target is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "нет прав на загрузку в этот домен")
    domain, user = target, user
    blob, meta = await docs_svc.store_and_probe(file.file, file.filename or "file")
    kind = archive.kind_of(meta.mime, meta.ext)
    result: dict = {"domains": doms, "picked": domain.slug}

    if kind:
        batch = UploadBatch(
            domain_id=domain.id,
            uploaded_by=user.id,
            source_filename=(file.filename or "archive")[:500],
            kind=BatchKind.archive,
            status="processing",
        )
        db.add(batch)
        await db.flush()
        batch_id = batch.id
        mode = "new" if on_conflict == "new" else (domain.settings or {}).get(
            "archive_on_conflict", "skip"
        )
        await db.commit()
        await process_archive(
            batch_id=batch_id,
            archive_sha256=blob.sha256,
            archive_kind=kind,
            domain_id=domain.id,
            uploader_id=user.id,
            conflict_mode=mode,
        )
        fresh = await db.get(UploadBatch, batch_id)
        await db.refresh(fresh)
        items = list(
            await db.scalars(select(UploadBatchItem).where(UploadBatchItem.batch_id == batch_id))
        )
        result["batch"] = fresh
        result["items"] = items
    else:
        try:
            ing = await docs_svc.ingest_upload(
                db, domain, user, original_name=file.filename or "file", blob=blob, meta=meta,
                on_conflict=on_conflict if on_conflict in ("replace", "new") else None,
            )
        except docs_svc.DisallowedType as err:
            result["error"] = f"Тип «{err.ext or err.mime}» не разрешён в этом домене."
        except docs_svc.NameConflict as err:
            result["conflict"] = str(err.existing_id)
        except docs_svc.QuotaExceeded:
            result["error"] = "Достигнут лимит хранилища домена."
        else:
            result["created"] = ing
            s = domain.settings or {}
            if s.get("auto_ocr") and ocr_engine.is_supported(ing.document.mime):
                ing.document.ocr_status = OcrStatus.pending
                await db.flush()
                await dispatch(None, "ocr_document", ocr_document, document_id=ing.document.id)
            if s.get("auto_index"):
                await index_document(db, ing.document)

    return render(request, "upload.html", result)


# --- document detail + edits --------------------------------------
async def _tag_names(db: AsyncSession, doc_id: uuid.UUID) -> list[str]:
    rows = await db.scalars(
        select(Tag.name)
        .join(DocumentTag, DocumentTag.tag_id == Tag.id)
        .where(DocumentTag.document_id == doc_id)
        .order_by(Tag.name)
    )
    return list(rows)


@router.get("/documents/{document_id}")
async def document_page(
    request: Request,
    document_id: uuid.UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    doc, view = await load_document(db, user, document_id)
    return render(
        request,
        "document.html",
        {
            "view": view,
            "doc": doc,
            "tags": await _tag_names(db, doc.id),
            "ocr_supported": ocr_engine.is_supported(doc.mime),
            "sets": await docsets_svc.list_sets(db, doc.domain_id, user.id),
            "partial": "_doc_body.html",
        },
    )


@router.post("/documents/{document_id}")
async def document_update(
    request: Request,
    document_id: uuid.UUID,
    title: str = Form(...),
    doc_date: str = Form(default=""),
    notes: str = Form(default=""),
    _: None = CsrfGuard,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    doc, view = await load_document(db, user, document_id)
    require_cap(view, Cap.write)
    parsed_date = None
    if doc_date:
        try:
            parsed_date = datetime.fromisoformat(doc_date)
        except ValueError:
            parsed_date = None
    await docs_svc.update_document(
        db, doc, title=title, doc_date=parsed_date, notes=notes,
        clear_doc_date=(not doc_date),
    )
    return RedirectResponse(f"/documents/{document_id}", status_code=303)


@router.post("/documents/{document_id}/tags")
async def document_tags(
    request: Request,
    document_id: uuid.UUID,
    tags: str = Form(default=""),
    _: None = CsrfGuard,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    doc, view = await load_document(db, user, document_id)
    require_cap(view, Cap.write)
    names = [p.strip() for p in tags.split(",") if p.strip()]
    tag_ids = []
    for name in names:
        tag = await tags_svc.get_or_create_tag(db, doc.domain_id, name, actor=user)
        tag_ids.append(tag.id)
    await tags_svc.set_document_tags(db, doc, tag_ids, actor=user)
    return await _doc_fragment(request, db, user, document_id)


@router.post("/documents/{document_id}/ocr")
async def document_ocr(
    request: Request,
    document_id: uuid.UUID,
    _: None = CsrfGuard,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    doc, view = await load_document(db, user, document_id)
    require_cap(view, Cap.process)
    if not ocr_engine.is_supported(doc.mime):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "OCR не поддерживает этот тип")
    doc.ocr_status = OcrStatus.pending
    await db.flush()
    await dispatch(None, "ocr_document", ocr_document, document_id=doc.id)
    return await _doc_fragment(request, db, user, document_id)


@router.post("/documents/{document_id}/index")
async def document_index(
    request: Request,
    document_id: uuid.UUID,
    _: None = CsrfGuard,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    doc, view = await load_document(db, user, document_id)
    require_cap(view, Cap.process)
    await index_document(db, doc)
    return await _doc_fragment(request, db, user, document_id)


@router.post("/documents/{document_id}/add-to-set")
async def document_add_to_set(
    request: Request,
    document_id: uuid.UUID,
    set_id: uuid.UUID = Form(...),
    _: None = CsrfGuard,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    doc, view = await load_document(db, user, document_id)
    s = await db.get(DocumentSet, set_id)
    if s is None or s.domain_id != doc.domain_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "набор не найден")
    if s.created_by != user.id and s.visibility != SetVisibility.domain:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "набор не найден")
    await docsets_svc.add_items(db, s, [doc.id], actor=user)
    return RedirectResponse(f"/documents/{document_id}", status_code=303)


@router.post("/documents/{document_id}/delete")
async def document_delete(
    document_id: uuid.UUID,
    _: None = CsrfGuard,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    doc, view = await load_document(db, user, document_id)
    require_cap(view, Cap.delete)
    await trash_svc.soft_delete(db, doc, user)
    return RedirectResponse(f"/domains/{view.domain.slug}/search", status_code=303)


@router.post("/documents/{document_id}/restore")
async def document_restore(
    request: Request,
    document_id: uuid.UUID,
    _: None = CsrfGuard,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    doc, view = await load_document(db, user, document_id)
    require_cap(view, Cap.delete)
    try:
        await trash_svc.restore(db, doc)
    except trash_svc.TrashError as err:
        raise HTTPException(status.HTTP_409_CONFLICT, str(err)) from err
    return RedirectResponse(f"/documents/{document_id}", status_code=303)


@router.get("/documents/{document_id}/download")
async def document_download(
    document_id: uuid.UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> FileResponse:
    doc, view = await load_document(db, user, document_id)
    require_cap(view, Cap.download)
    path = storage.blob_path(doc.sha256)
    if not path.is_file():
        raise HTTPException(status.HTTP_410_GONE, "файл отсутствует в хранилище")
    return FileResponse(path, media_type=doc.mime, filename=doc.original_name)


async def _doc_fragment(request: Request, db, user, document_id: uuid.UUID) -> Response:
    # Persist this request's changes, then re-read so an inline job's commit
    # (a separate session, e.g. OCR) is reflected and all columns are loaded
    # before the synchronous template render.
    await db.commit()
    doc, view = await load_document(db, user, document_id)
    await db.refresh(doc)
    return render(
        request,
        "_doc_body.html",
        {
            "view": view,
            "doc": doc,
            "tags": await _tag_names(db, doc.id),
            "ocr_supported": ocr_engine.is_supported(doc.mime),
            "sets": await docsets_svc.list_sets(db, doc.domain_id, user.id),
        },
    )
