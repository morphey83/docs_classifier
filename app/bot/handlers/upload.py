"""Inbound files & archives → the current domain's inbox."""

from __future__ import annotations

import io

from aiogram import Bot, F, Router
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import state as state_svc
from app.bot.handlers._util import needs_link
from app.ingest import archive
from app.jobs import dispatch
from app.models import BatchKind, Document, Domain, UploadBatch, UploadBatchItem, User
from app.ocr import engine as ocr_engine
from app.services import documents as docs_svc
from app.services import domains as domains_svc
from app.services.ingest import process_archive
from app.services.search import index_document

router = Router(name="upload")

_TG_MAX = 20 * 1024 * 1024  # Bot API download cap


async def _current_domain(db: AsyncSession, user: User) -> Domain | None:
    domain_id = await state_svc.current_domain_id(db, user.id)
    if domain_id is not None:
        row = await domains_svc.get_membership(db, domain_id, user.id)
        if row is not None:
            return row[0]
    memberships = await domains_svc.list_memberships(db, user)
    if len(memberships) == 1:
        return memberships[0][0]
    return None


@router.message(F.document | F.photo)
async def on_file(message: Message, bot: Bot, db: AsyncSession, user: User | None) -> None:
    if user is None:
        await needs_link(message)
        return
    domain = await _current_domain(db, user)
    if domain is None:
        await message.answer("Сначала выберите домен: /domain")
        return

    if message.document is not None:
        src = message.document
        filename = src.file_name or f"file-{src.file_unique_id}"
        size = src.file_size or 0
    else:
        src = message.photo[-1]
        filename = f"photo-{src.file_unique_id}.jpg"
        size = src.file_size or 0

    if size > _TG_MAX:
        await message.answer("Файл больше 20 МБ — загрузите через веб-интерфейс.")
        return

    buf = io.BytesIO()
    await bot.download(src, destination=buf)
    buf.seek(0)

    blob, meta = await docs_svc.store_and_probe(buf, filename)
    kind = archive.kind_of(meta.mime, meta.ext)

    if kind:
        await _ingest_archive(message, db, domain, user, blob, kind)
        return

    try:
        result = await docs_svc.ingest_upload(
            db, domain, user, original_name=filename, blob=blob, meta=meta
        )
    except docs_svc.DisallowedType as err:
        await message.answer(
            f"Тип «{err.ext or err.mime}» не разрешён в домене «{domain.name}»."
        )
        return
    except docs_svc.NameConflict:
        await message.answer(
            "Документ с таким именем уже есть, но с другим содержимым. "
            "Переименуйте файл или замените документ через веб."
        )
        return
    except docs_svc.QuotaExceeded:
        await message.answer("Достигнут лимит хранилища домена.")
        return

    await _maybe_auto_process(db, domain, result.document)
    await message.answer(
        f"✅ «{result.document.title}» → инбокс домена «{domain.name}» ({result.outcome})."
    )


async def _ingest_archive(message, db, domain, user, blob, kind) -> None:
    batch = UploadBatch(
        domain_id=domain.id,
        uploaded_by=user.id,
        source_filename="archive"[:500],
        kind=BatchKind.archive,
        status="processing",
    )
    db.add(batch)
    await db.flush()
    batch_id = batch.id
    mode = (domain.settings or {}).get("archive_on_conflict", "skip")
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
    skipped = [i for i in items if i.outcome in ("skipped", "skipped_type", "error")]
    lines = [
        f"Архив обработан: добавлено {fresh.item_count}, "
        f"конфликтов {fresh.conflict_count}, пропущено {len(skipped)}."
    ]
    for i in skipped[:15]:
        lines.append(f"• {i.entry_name} — {i.note or i.outcome}")
    await message.answer("\n".join(lines))


async def _maybe_auto_process(db: AsyncSession, domain: Domain, doc: Document) -> None:
    settings_ = domain.settings or {}
    if settings_.get("auto_ocr") and ocr_engine.is_supported(doc.mime):
        from app.models import OcrStatus
        from app.ocr.tasks import ocr_document

        doc.ocr_status = OcrStatus.pending
        await db.flush()
        await dispatch(None, "ocr_document", ocr_document, document_id=doc.id)
    if settings_.get("auto_index"):
        await index_document(db, doc)
