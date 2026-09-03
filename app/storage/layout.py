"""Local-only path helpers for the regenerable caches.

``derived`` (thumbnails, OCR sidecars) and ``artifacts`` (export / set-archive
zips) still resolve to real directories under ``DATA_DIR``. They are caches —
losing them only forces a rebuild — so phase 1 leaves them on local disk. A
later phase routes them through :class:`ObjectStore` like blobs.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from app.config import settings


def _derived_root() -> Path:
    return settings.data_dir / "derived"


def derived_dir(sha256: str) -> Path:
    d = _derived_root() / sha256[:2] / sha256[2:4] / sha256
    d.mkdir(parents=True, exist_ok=True)
    return d


def remove_derived(sha256: str) -> None:
    """Delete the derived-files directory (OCR sidecars, …) for a blob, if any."""
    d = _derived_root() / sha256[:2] / sha256[2:4] / sha256
    if d.is_dir():
        shutil.rmtree(d, ignore_errors=True)


def artifacts_dir() -> Path:
    d = settings.data_dir / "artifacts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def artifact_path(artifact_id: str) -> Path:
    return artifacts_dir() / f"{artifact_id}.zip"


def set_archive_name(set_id: str) -> str:
    """Stable file name for a document set's archive cache (§15)."""
    return f"set-{set_id}.zip"


def set_archive_path(set_id: str) -> Path:
    return artifacts_dir() / set_archive_name(str(set_id))
