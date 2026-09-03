"""The daily ``cleanup`` job — trash retention, expired artifacts, orphan blobs.

Wired as a SAQ cron job in ``app/worker.py``; also callable directly (tests /
a manual admin trigger).
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from app import storage
from app.config import settings
from app.db import get_sessionmaker
from app.models import (
    Artifact,
    ArtifactKind,
    ArtifactStatus,
    Document,
    DocumentVersion,
    Domain,
)
from app.services import tags as tags_svc
from app.services import trash
from app.util.time import as_aware, utcnow


def _artifact_file(artifact: Artifact):
    if not artifact.storage_key:
        return None
    return storage.artifacts_dir() / artifact.storage_key


async def run_cleanup() -> dict[str, int]:
    stats = {
        "purged_documents": 0,
        "deleted_exports": 0,
        "cleared_set_archives": 0,
        "orphan_blobs": 0,
        "orphan_tags": 0,
    }
    sm = get_sessionmaker()
    async with sm() as db:
        now = utcnow()

        # 1. trash past each domain's retention window -> hard purge
        domains = list(await db.scalars(select(Domain)))
        for domain in domains:
            days = int(
                (domain.settings or {}).get(
                    "trash_retention_days", settings.default_trash_retention_days
                )
            )
            cutoff = now - timedelta(days=days)
            stale = list(
                await db.scalars(
                    select(Document).where(
                        Document.domain_id == domain.id,
                        Document.deleted_at.is_not(None),
                        Document.deleted_at < cutoff,
                    )
                )
            )
            for doc in stale:
                await trash.hard_purge(db, doc)
                stats["purged_documents"] += 1

        # 2. expired ad-hoc exports -> row + file gone (snapshots, not rebuildable)
        for art in await db.scalars(
            select(Artifact).where(
                Artifact.kind == ArtifactKind.adhoc_export,
                Artifact.expires_at.is_not(None),
            )
        ):
            if as_aware(art.expires_at) <= now:
                f = _artifact_file(art)
                if f is not None:
                    f.unlink(missing_ok=True)
                await db.delete(art)
                stats["deleted_exports"] += 1

        # 3. expired set-archive files -> drop the file, keep row + links so the
        #    next access rebuilds (§15)
        for art in await db.scalars(
            select(Artifact).where(
                Artifact.kind == ArtifactKind.set_archive,
                Artifact.storage_key.is_not(None),
                Artifact.expires_at.is_not(None),
            )
        ):
            if as_aware(art.expires_at) <= now:
                f = _artifact_file(art)
                if f is not None:
                    f.unlink(missing_ok=True)
                art.storage_key = None
                art.size_bytes = 0
                art.status = ArtifactStatus.building
                stats["cleared_set_archives"] += 1

        # 3b. tags no document references any more (§7 rev 2)
        stats["orphan_tags"] = await tags_svc.sweep_orphan_tags(db)

        await db.commit()

        # 4. orphan blob sweep (refcount 0 across document + document_version)
        on_disk = set(await run_in_threadpool(storage.list_blob_hashes))
        if on_disk:
            referenced = set(await db.scalars(select(Document.sha256.distinct())))
            referenced |= set(await db.scalars(select(DocumentVersion.sha256.distinct())))
            for h in on_disk - referenced:
                await run_in_threadpool(_purge_blob, h)
                stats["orphan_blobs"] += 1

    return stats


def _purge_blob(sha256: str) -> None:
    storage.delete_blob(sha256)
    storage.remove_derived(sha256)
