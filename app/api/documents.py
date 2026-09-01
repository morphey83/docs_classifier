"""Upload, batches, document CRUD, tagging, indexing, search, inbox."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.api._common import document_out
from app.db import get_session
from app.deps import DocCtx, DomainCtx, require, require_doc
from app.ingest import archive
from app.jobs import dispatch
from app.models import (
    BatchKind,
    DocStatus,
    Document,
    OcrStatus,
    TextSource,
    UploadBatch,
    UploadBatchItem,
)
from app.ocr import engine as ocr_engine
from app.ocr.tasks import ocr_document
from app.rbac import Cap
from app.schemas.documents import (
    DocumentList,
    DocumentOut,
    DocumentTagsUpdate,
    DocumentUpdate,
    Facets,
    InboxStatus,
    UploadResult,
)
from app.schemas.uploads import BatchDetail, BatchItemOut, BatchOut
from app.services import documents as svc
from app.services import search as search_svc
from app.services import tags as tags_svc
from app.services.ingest import process_archive


async def _maybe_auto_ocr(background, db, domain, doc: Document) -> None:
    if (domain.settings or {}).get("auto_ocr") and ocr_engine.is_supported(doc.mime):
        doc.ocr_status = OcrStatus.pending
        await db.flush()
        await dispatch(background, "ocr_document", ocr_document, document_id=doc.id)


router = APIRouter(tags=["documents"])


# --- upload ---------------------------------------------------------------
@router.post("/domains/{domain_id}/uploads", status_code=status.HTTP_201_CREATED)
async def upload(
    background: BackgroundTasks,
    file: UploadFile,
    on_conflict: svc.OnConflict | None = Query(default=None),
    ctx: DomainCtx = Depends(require(Cap.upload)),
    db: AsyncSession = Depends(get_session),
):
    blob, meta = await svc.store_and_probe(file.file, file.filename or "file")
    kind = archive.kind_of(meta.mime, meta.ext)

    if kind:
        batch = UploadBatch(
            domain_id=ctx.domain.id,
            uploaded_by=ctx.user.id,
            source_filename=(file.filename or "archive")[:500],
            kind=BatchKind.archive,
            status="processing",
        )
        db.add(batch)
        await db.flush()
        mode = (
            "new"
            if on_conflict == "new"
            else ((ctx.domain.settings or {}).get("archive_on_conflict", "skip"))
        )
        await db.commit()  # persist the batch before the job (queue mode) reads it
        await dispatch(
            background,
            "process_archive",
            process_archive,
            batch_id=batch.id,
            archive_sha256=blob.sha256,
            archive_kind=kind,
            domain_id=ctx.domain.id,
            uploader_id=ctx.user.id,
            conflict_mode=mode,
        )
        return Response(
            content=BatchOut.model_validate(batch).model_dump_json(),
            media_type="application/json",
            status_code=status.HTTP_202_ACCEPTED,
        )

    try:
        result = await svc.ingest_upload(
            db,
            ctx.domain,
            ctx.user,
            original_name=file.filename or "file",
            blob=blob,
            meta=meta,
            on_conflict=on_conflict,
        )
    except svc.NameConflict as err:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "error": "name_conflict",
                "existing_id": str(err.existing_id),
                "resolve_with": ["?on_conflict=replace", "?on_conflict=new"],
            },
        ) from err
    except svc.QuotaExceeded as err:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={"error": "quota_exceeded", "used": err.used, "quota": err.quota},
        ) from err
    if result.outcome in ("created", "new_from_conflict", "replaced"):
        await _maybe_auto_ocr(background, db, ctx.domain, result.document)
    return UploadResult(outcome=result.outcome, document=await document_out(db, result.document))


@router.get("/domains/{domain_id}/uploads", response_model=list[BatchOut])
async def list_batches(
    ctx: DomainCtx = Depends(require(Cap.view)), db: AsyncSession = Depends(get_session)
) -> list[BatchOut]:
    rows = await db.scalars(
        select(UploadBatch)
        .where(UploadBatch.domain_id == ctx.domain.id)
        .order_by(UploadBatch.uploaded_at.desc())
        .limit(200)
    )
    return [BatchOut.model_validate(b) for b in rows]


@router.get("/domains/{domain_id}/uploads/{batch_id}", response_model=BatchDetail)
async def get_batch(
    batch_id: uuid.UUID,
    ctx: DomainCtx = Depends(require(Cap.view)),
    db: AsyncSession = Depends(get_session),
) -> BatchDetail:
    batch = await db.get(UploadBatch, batch_id)
    if batch is None or batch.domain_id != ctx.domain.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "batch not found")
    items = await db.scalars(select(UploadBatchItem).where(UploadBatchItem.batch_id == batch.id))
    return BatchDetail(
        **BatchOut.model_validate(batch).model_dump(),
        items=[BatchItemOut.model_validate(i) for i in items],
    )


# --- search --------------------------------------------------------------
def _csv(value: str | None) -> list[str]:
    return [s.strip() for s in value.split(",") if s.strip()] if value else []


@router.get("/domains/{domain_id}/documents", response_model=DocumentList)
async def search_documents(
    q: str | None = Query(default=None),
    status_: DocStatus | None = Query(default=None, alias="status"),
    tags: str | None = Query(default=None, description="tag slugs, comma-sep (all must match)"),
    tags_any: str | None = Query(default=None),
    tags_none: str | None = Query(default=None),
    ext: str | None = Query(default=None),
    mime: str | None = Query(default=None),
    size_min: int | None = Query(default=None, ge=0),
    size_max: int | None = Query(default=None, ge=0),
    doc_date_from: datetime | None = None,
    doc_date_to: datetime | None = None,
    uploaded_from: datetime | None = None,
    uploaded_to: datetime | None = None,
    uploaded_by: uuid.UUID | None = None,
    has_index: bool | None = None,
    has_ocr: bool | None = None,
    text_source: TextSource | None = None,
    include_trash: bool = Query(default=False),
    sort: str = Query(default="uploaded_at"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    facets: bool = Query(default=True),
    ctx: DomainCtx = Depends(require(Cap.view)),
    db: AsyncSession = Depends(get_session),
) -> DocumentList:
    f = search_svc.SearchFilters(
        q=q,
        status=status_,
        tags_all=_csv(tags),
        tags_any=_csv(tags_any),
        tags_none=_csv(tags_none),
        ext=ext,
        mime=mime,
        size_min=size_min,
        size_max=size_max,
        doc_date_from=doc_date_from,
        doc_date_to=doc_date_to,
        uploaded_from=uploaded_from,
        uploaded_to=uploaded_to,
        uploaded_by=uploaded_by,
        has_index=has_index,
        has_ocr=has_ocr,
        text_source=text_source,
        include_trash=include_trash,
        sort=sort,
        page=page,
        page_size=page_size,
    )
    docs, total, facet_data = await search_svc.search_documents(db, ctx.domain.id, f)
    return DocumentList(
        items=[await document_out(db, d) for d in docs],
        total=total,
        page=page,
        page_size=page_size,
        facets=Facets(**facet_data.__dict__) if facets else None,
    )


# --- inbox ---------------------------------------------------------------
@router.get("/domains/{domain_id}/inbox", response_model=InboxStatus)
async def inbox_status(
    ctx: DomainCtx = Depends(require(Cap.view)), db: AsyncSession = Depends(get_session)
) -> InboxStatus:
    return InboxStatus(count=await svc.inbox_count(db, ctx.domain.id))


@router.get("/domains/{domain_id}/inbox/next", response_model=DocumentOut | None)
async def inbox_next(
    ctx: DomainCtx = Depends(require(Cap.write)), db: AsyncSession = Depends(get_session)
) -> DocumentOut | None:
    doc = await svc.next_inbox_document(db, ctx.domain.id, ctx.user.id)
    return await document_out(db, doc) if doc else None


@router.post("/domains/{domain_id}/inbox/undefer")
async def inbox_undefer(
    ctx: DomainCtx = Depends(require(Cap.write)), db: AsyncSession = Depends(get_session)
) -> dict[str, int]:
    return {"cleared": await svc.clear_defers(db, ctx.domain.id, ctx.user.id)}


# --- one document -------------------------------------------------------
@router.get("/documents/{document_id}", response_model=DocumentOut)
async def get_document(
    ctx: DocCtx = Depends(require_doc(Cap.view)), db: AsyncSession = Depends(get_session)
) -> DocumentOut:
    return await document_out(db, ctx.document)


@router.patch("/documents/{document_id}", response_model=DocumentOut)
async def update_document(
    body: DocumentUpdate,
    ctx: DocCtx = Depends(require_doc(Cap.write)),
    db: AsyncSession = Depends(get_session),
) -> DocumentOut:
    doc = await svc.update_document(
        db,
        ctx.document,
        title=body.title,
        doc_date=body.doc_date,
        notes=body.notes,
        clear_doc_date=body.clear_doc_date,
    )
    return await document_out(db, doc)


@router.get("/documents/{document_id}/content")
async def download_document(
    ctx: DocCtx = Depends(require_doc(Cap.download)),
) -> FileResponse:
    path = storage.blob_path(ctx.document.sha256)
    if not path.is_file():
        raise HTTPException(status.HTTP_410_GONE, "blob missing from storage")
    return FileResponse(path, media_type=ctx.document.mime, filename=ctx.document.original_name)


@router.post("/documents/{document_id}/index", response_model=DocumentOut)
async def index_document(
    reparse: bool = Query(default=False),
    ctx: DocCtx = Depends(require_doc(Cap.process)),
    db: AsyncSession = Depends(get_session),
) -> DocumentOut:
    await search_svc.index_document(db, ctx.document, reparse=reparse)
    return await document_out(db, ctx.document)


@router.post("/documents/{document_id}/ocr", response_model=DocumentOut)
async def request_ocr(
    background: BackgroundTasks,
    lang: str | None = Query(default=None, description="e.g. rus+eng"),
    ctx: DocCtx = Depends(require_doc(Cap.process)),
    db: AsyncSession = Depends(get_session),
) -> DocumentOut:
    if not ocr_engine.is_supported(ctx.document.mime):
        ctx.document.ocr_status = OcrStatus.unsupported
        await db.commit()
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"OCR does not support {ctx.document.mime}",
        )
    ctx.document.ocr_status = OcrStatus.pending
    await db.flush()
    await dispatch(background, "ocr_document", ocr_document, document_id=ctx.document.id, lang=lang)
    return await document_out(db, ctx.document)


@router.patch("/documents/{document_id}/tags", response_model=DocumentOut)
async def set_tags(
    body: DocumentTagsUpdate,
    ctx: DocCtx = Depends(require_doc(Cap.write)),
    db: AsyncSession = Depends(get_session),
) -> DocumentOut:
    tag_ids: list[uuid.UUID] = list(body.tag_ids or [])
    for name in body.tag_names or []:
        tag = await tags_svc.get_or_create_tag(db, ctx.document.domain_id, name, actor=ctx.user)
        tag_ids.append(tag.id)
    try:
        await tags_svc.set_document_tags(db, ctx.document, tag_ids, actor=ctx.user)
    except tags_svc.TagError as err:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(err)) from err
    return await document_out(db, ctx.document)


@router.post("/documents/{document_id}/complete", response_model=DocumentOut)
async def complete(
    ctx: DocCtx = Depends(require_doc(Cap.write)),
    db: AsyncSession = Depends(get_session),
) -> DocumentOut:
    await svc.complete_document(db, ctx.document)
    return await document_out(db, ctx.document)


@router.post("/documents/{document_id}/defer")
async def defer(
    ctx: DocCtx = Depends(require_doc(Cap.write)),
    db: AsyncSession = Depends(get_session),
) -> Response:
    await svc.defer_document(db, ctx.document, ctx.user.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
