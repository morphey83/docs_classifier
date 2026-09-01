"""Archive ingest — runs as a background task with its own DB session."""

from __future__ import annotations

import uuid
from pathlib import Path

from starlette.concurrency import run_in_threadpool

from app import storage
from app.config import settings
from app.db import get_sessionmaker
from app.ingest.archive import ArchiveError, Limits, iter_archive
from app.models import DocSource, Domain, UploadBatch, UploadBatchItem, User
from app.services.documents import (
    NameConflict,
    QuotaExceeded,
    ingest_upload,
)

ArchiveConflictMode = str  # "skip" | "new"


def _limits() -> Limits:
    return Limits(
        max_entries=settings.max_archive_entries,
        max_total_bytes=settings.max_archive_unpacked_mb * 1024 * 1024,
        max_entry_bytes=settings.max_upload_mb * 1024 * 1024,
        max_depth=settings.max_archive_depth,
    )


async def process_archive(
    *,
    batch_id: uuid.UUID,
    archive_sha256: str,
    archive_kind: str,
    domain_id: uuid.UUID,
    uploader_id: uuid.UUID,
    conflict_mode: ArchiveConflictMode,
) -> None:
    sm = get_sessionmaker()
    async with sm() as db:
        batch = await db.get(UploadBatch, batch_id)
        domain = await db.get(Domain, domain_id)
        uploader = await db.get(User, uploader_id)
        if batch is None or domain is None or uploader is None:  # pragma: no cover
            return

        arc_path = storage.blob_path(archive_sha256)
        try:
            entries: list[tuple[str, Path]] = await run_in_threadpool(
                lambda: list(iter_archive(arc_path, archive_kind, _limits()))
            )
        except ArchiveError as err:
            batch.status = "failed"
            batch.error = str(err)[:2000]
            await db.commit()
            return

        created = conflicts = errors = 0
        stop = False
        for entry_name, tmp in entries:
            if stop:
                tmp.unlink(missing_ok=True)
                continue
            try:
                with tmp.open("rb") as fh:
                    res = await ingest_upload(
                        db,
                        domain,
                        uploader,
                        stream=fh,
                        original_name=entry_name,
                        on_conflict="new" if conflict_mode == "new" else None,
                        source=DocSource.archive,
                        batch_id=batch.id,
                    )
                db.add(
                    UploadBatchItem(
                        batch_id=batch.id,
                        entry_name=entry_name,
                        outcome=res.outcome,
                        document_id=res.document.id,
                    )
                )
                if res.outcome in ("created", "new_from_conflict"):
                    created += 1
            except NameConflict as err:
                conflicts += 1
                db.add(
                    UploadBatchItem(
                        batch_id=batch.id,
                        entry_name=entry_name,
                        outcome="skipped",
                        note=f"name conflict with document {err.existing_id}",
                    )
                )
            except QuotaExceeded:
                errors += 1
                stop = True
                db.add(
                    UploadBatchItem(
                        batch_id=batch.id,
                        entry_name=entry_name,
                        outcome="error",
                        note="domain storage quota exceeded",
                    )
                )
            except Exception as err:
                errors += 1
                db.add(
                    UploadBatchItem(
                        batch_id=batch.id,
                        entry_name=entry_name,
                        outcome="error",
                        note=str(err)[:500],
                    )
                )
            finally:
                tmp.unlink(missing_ok=True)
            await db.flush()

        batch.item_count = created
        batch.conflict_count = conflicts
        batch.status = "done" if errors == 0 and not stop else "partial"
        await db.commit()
