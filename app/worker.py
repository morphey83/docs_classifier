"""SAQ worker — runs the job queue on Postgres (no Redis).

``saq app.worker.settings_dict``
"""

from __future__ import annotations

from typing import Any

from app.jobs import get_queue
from app.ocr.tasks import ocr_document
from app.services.export import build_artifact as _build_artifact
from app.services.ingest import process_archive as _process_archive


async def build_artifact(ctx: dict[str, Any], *, artifact_id: str) -> None:
    await _build_artifact(artifact_id)


async def process_archive(ctx: dict[str, Any], **kwargs: Any) -> None:
    await _process_archive(**kwargs)


settings_dict = {
    "queue": get_queue(),
    "functions": [ocr_document, build_artifact, process_archive],
    "concurrency": 2,
}
