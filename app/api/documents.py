"""Upload, document CRUD, tagging, and the inbox queue."""

from __future__ import annotations

import uuid

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.api._common import document_out
from app.db import get_session
from app.deps import DocCtx, DomainCtx, require, require_doc
from app.models import DocStatus
from app.rbac import Cap
from app.schemas.documents import (
    DocumentList,
    DocumentOut,
    DocumentTagsUpdate,
    DocumentUpdate,
    InboxStatus,
    UploadResult,
)
from app.services import documents as svc
from app.services import tags as tags_svc

router = APIRouter(tags=["documents"])


@router.post(
    "/domains/{domain_id}/uploads",
    response_model=UploadResult,
    status_code=status.HTTP_201_CREATED,
)
async def upload(
    file: UploadFile,
    on_conflict: svc.OnConflict | None = Query(default=None),
    ctx: DomainCtx = Depends(require(Cap.upload)),
    db: AsyncSession = Depends(get_session),
) -> UploadResult:
    try:
        result = await svc.ingest_upload(
            db,
            ctx.domain,
            ctx.user,
            stream=file.file,
            original_name=file.filename or "file",
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
    return UploadResult(
        outcome=result.outcome, document=await document_out(db, result.document)
    )


@router.get("/domains/{domain_id}/documents", response_model=DocumentList)
async def list_documents(
    status_: DocStatus | None = Query(default=None, alias="status"),
    tags: str | None = Query(default=None, description="comma-separated tag slugs (AND)"),
    q: str | None = Query(default=None),
    include_trash: bool = Query(default=False),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    ctx: DomainCtx = Depends(require(Cap.view)),
    db: AsyncSession = Depends(get_session),
) -> DocumentList:
    slugs = [s.strip() for s in tags.split(",") if s.strip()] if tags else None
    items, total = await svc.list_documents(
        db,
        ctx.domain.id,
        status=status_,
        tag_slugs=slugs,
        q=q,
        include_trash=include_trash,
        page=page,
        page_size=page_size,
    )
    return DocumentList(
        items=[await document_out(db, d) for d in items],
        total=total,
        page=page,
        page_size=page_size,
    )


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
    return FileResponse(
        path, media_type=ctx.document.mime, filename=ctx.document.original_name
    )


@router.patch("/documents/{document_id}/tags", response_model=DocumentOut)
async def set_tags(
    body: DocumentTagsUpdate,
    ctx: DocCtx = Depends(require_doc(Cap.write)),
    db: AsyncSession = Depends(get_session),
) -> DocumentOut:
    tag_ids: list[uuid.UUID] = list(body.tag_ids or [])
    for name in body.tag_names or []:
        tag = await tags_svc.get_or_create_tag(
            db, ctx.document.domain_id, name, actor=ctx.user
        )
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
