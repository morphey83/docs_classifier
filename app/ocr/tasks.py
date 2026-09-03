"""The ``ocr_document`` job — runs a document through OCR and re-indexes it."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from starlette.concurrency import run_in_threadpool

from app import storage
from app.config import settings
from app.db import get_sessionmaker
from app.models import Document, OcrStatus, TextSource
from app.ocr import engine
from app.services.search import index_document
from app.util.time import utcnow

_LOGGER = logging.getLogger(__name__)


async def ocr_document(
    ctx: Any = None, *, document_id: str | uuid.UUID, lang: str | None = None
) -> None:
    doc_id = uuid.UUID(str(document_id))
    sm = get_sessionmaker()
    async with sm() as db:
        doc = await db.get(Document, doc_id)
        if doc is None or doc.deleted_at is not None:  # pragma: no cover
            return

        if not engine.is_supported(doc.mime):
            doc.ocr_status = OcrStatus.unsupported
            await db.commit()
            return

        lang = lang or settings.ocr_default_lang
        try:
            with storage.blobs_store().open_local(storage.blob_key(doc.sha256)) as blob:
                result = await run_in_threadpool(engine.run_ocr, blob, doc.mime, lang)
        except Exception:
            _LOGGER.exception("OCR failed for document %s", doc_id)
            doc.ocr_status = OcrStatus.failed
            doc.ocr_lang = lang
            await db.commit()
            return

        text = (result.text or "").strip()
        if result.sidecar_pdf:
            (storage.derived_dir(doc.sha256) / "ocr.pdf").write_bytes(result.sidecar_pdf)

        if text:
            doc.extracted_text = text
            doc.text_source = TextSource.ocr
        doc.ocr_status = OcrStatus.done
        doc.ocr_at = utcnow()
        doc.ocr_lang = lang
        await db.flush()

        # refresh the search vector with the OCR'd text
        await index_document(db, doc, reparse=False)
        await db.commit()
