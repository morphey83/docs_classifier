"""Build export archives (artifacts): a zip of the originals + a manifest."""

from __future__ import annotations

import csv
import io
import json
import uuid
import zipfile
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app import storage
from app.config import settings
from app.db import get_sessionmaker
from app.models import (
    Artifact,
    ArtifactKind,
    ArtifactStatus,
    Document,
    Domain,
    Tag,
    User,
)
from app.services.search import SearchFilters, search_documents
from app.util.time import utcnow


async def create_export(
    db: AsyncSession,
    domain: Domain,
    user: User,
    *,
    filters: SearchFilters | None = None,
    doc_ids: list[uuid.UUID] | None = None,
) -> Artifact:
    if doc_ids is None:
        f = filters or SearchFilters()
        f.page = 1
        f.page_size = 100_000
        docs, _total, _facets = await search_documents(db, domain.id, f)
        ids = [d.id for d in docs]
    else:
        rows = await db.scalars(
            select(Document.id).where(
                Document.domain_id == domain.id,
                Document.id.in_(doc_ids),
                Document.deleted_at.is_(None),
            )
        )
        ids = list(rows)

    artifact = Artifact(
        domain_id=domain.id,
        kind=ArtifactKind.adhoc_export,
        status=ArtifactStatus.building,
        item_count=len(ids),
        snapshot={"doc_ids": [str(i) for i in ids]},
        requested_by=user.id,
        expires_at=utcnow() + timedelta(hours=settings.export_ttl_hours),
    )
    db.add(artifact)
    await db.flush()
    return artifact


async def build_artifact(artifact_id: uuid.UUID) -> None:
    sm = get_sessionmaker()
    async with sm() as db:
        artifact = await db.get(Artifact, artifact_id)
        if artifact is None:  # pragma: no cover
            return
        ids = [uuid.UUID(i) for i in (artifact.snapshot or {}).get("doc_ids", [])]
        docs = list(
            await db.scalars(
                select(Document).where(Document.id.in_(ids), Document.deleted_at.is_(None))
            )
        )
        tag_map = await _tags_by_document(db, [d.id for d in docs])

        try:
            key, size = await run_in_threadpool(_write_zip, str(artifact.id), docs, tag_map)
        except Exception as err:
            artifact.status = ArtifactStatus.failed
            artifact.error = str(err)[:2000]
            await db.commit()
            return

        artifact.storage_key = key
        artifact.size_bytes = size
        artifact.item_count = len(docs)
        artifact.missing_count = len(ids) - len(docs)
        artifact.status = ArtifactStatus.ready
        await db.commit()


async def _tags_by_document(
    db: AsyncSession, doc_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[str]]:
    from app.models import DocumentTag

    rows = await db.execute(
        select(DocumentTag.document_id, Tag.name)
        .join(Tag, Tag.id == DocumentTag.tag_id)
        .where(DocumentTag.document_id.in_(doc_ids))
    )
    out: dict[uuid.UUID, list[str]] = {}
    for did, name in rows:
        out.setdefault(did, []).append(name)
    return out


def _write_zip(artifact_id: str, docs: list[Document], tag_map: dict) -> tuple[str, int]:
    path = storage.artifact_path(artifact_id)
    used: set[str] = set()
    manifest: list[dict] = []

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for doc in docs:
            name = _dedup_name(doc.original_name, used)
            blob = storage.blob_path(doc.sha256)
            if blob.is_file():
                zf.write(blob, arcname=f"files/{name}")
            entry = {
                "id": str(doc.id),
                "file": f"files/{name}",
                "title": doc.title,
                "original_name": doc.original_name,
                "sha256": doc.sha256,
                "size_bytes": doc.size_bytes,
                "mime": doc.mime,
                "doc_date": doc.doc_date.isoformat() if doc.doc_date else None,
                "status": str(doc.status),
                "tags": sorted(tag_map.get(doc.id, [])),
            }
            manifest.append(entry)

        zf.writestr(
            "manifest.json",
            json.dumps(
                {"count": len(manifest), "documents": manifest},
                ensure_ascii=False,
                indent=2,
            ),
        )
        cols = [
            "id",
            "file",
            "title",
            "original_name",
            "doc_date",
            "size_bytes",
            "mime",
            "status",
            "tags",
        ]
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(cols)
        for e in manifest:
            w.writerow(
                [
                    e["id"],
                    e["file"],
                    e["title"],
                    e["original_name"],
                    e["doc_date"] or "",
                    e["size_bytes"],
                    e["mime"],
                    e["status"],
                    "; ".join(e["tags"]),
                ]
            )
        zf.writestr("manifest.csv", buf.getvalue())

    return path.name, path.stat().st_size


def _dedup_name(name: str, used: set[str]) -> str:
    from pathlib import PurePosixPath

    base = PurePosixPath(name.replace("\\", "/")).name or "file"
    if base not in used:
        used.add(base)
        return base
    stem, _, ext = base.rpartition(".")
    stem = stem or base
    n = 2
    while True:
        cand = f"{stem} ({n}).{ext}" if ext and ext != base else f"{base} ({n})"
        if cand not in used:
            used.add(cand)
            return cand
        n += 1
