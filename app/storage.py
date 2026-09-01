"""Content-addressed blob storage on the local filesystem.

Layout: ``DATA_DIR/blobs/<h[0:2]>/<h[2:4]>/<h>`` where ``h`` is the SHA-256 hex
digest. Identical content is stored once regardless of how many documents (or
domains) reference it.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from app.config import settings

_CHUNK = 1 << 20


@dataclass(frozen=True)
class BlobInfo:
    sha256: str
    size: int
    created: bool  # False if the blob already existed (dedup)

    @property
    def storage_key(self) -> str:
        h = self.sha256
        return f"{h[:2]}/{h[2:4]}/{h}"


def _blobs_root() -> Path:
    return settings.data_dir / "blobs"


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


def derived_dir(sha256: str) -> Path:
    d = settings.data_dir / "derived" / sha256[:2] / sha256[2:4] / sha256
    d.mkdir(parents=True, exist_ok=True)
    return d


def blob_path(sha256: str) -> Path:
    return _blobs_root() / sha256[:2] / sha256[2:4] / sha256


def blob_exists(sha256: str) -> bool:
    return blob_path(sha256).is_file()


def store_stream(src: BinaryIO) -> BlobInfo:
    """Hash ``src`` while streaming it to a temp file, then atomically place it."""
    root = _blobs_root()
    root.mkdir(parents=True, exist_ok=True)
    hasher = hashlib.sha256()
    size = 0
    fd, tmp_name = tempfile.mkstemp(dir=root, prefix=".incoming-")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as tmp_f:
            while chunk := src.read(_CHUNK):
                hasher.update(chunk)
                size += len(chunk)
                tmp_f.write(chunk)
        digest = hasher.hexdigest()
        dest = blob_path(digest)
        if dest.is_file():
            tmp.unlink(missing_ok=True)
            return BlobInfo(digest, size, created=False)
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(tmp, dest)
        except OSError:
            shutil.move(str(tmp), str(dest))
        return BlobInfo(digest, size, created=True)
    finally:
        tmp.unlink(missing_ok=True)


def store_bytes(data: bytes) -> BlobInfo:
    import io

    return store_stream(io.BytesIO(data))


def open_blob(sha256: str) -> BinaryIO:
    return blob_path(sha256).open("rb")


def delete_blob(sha256: str) -> None:
    blob_path(sha256).unlink(missing_ok=True)
