"""Content-addressed blob storage — the layer above :class:`ObjectStore`.

A blob is keyed by the SHA-256 hex digest of its content, laid out as
``<h[0:2]>/<h[2:4]>/<h>``. Identical content is stored once regardless of how
many documents (or domains) reference it. This module owns the hash-while-write
and the dedup check; the backend underneath just moves bytes.
"""

from __future__ import annotations

import hashlib
import io
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from app.storage.base import ObjectStore
from app.storage.registry import blobs_store

_CHUNK = 1 << 20


def blob_key(sha256: str) -> str:
    h = sha256
    return f"{h[:2]}/{h[2:4]}/{h}"


@dataclass(frozen=True)
class BlobInfo:
    sha256: str
    size: int
    created: bool  # False if the blob already existed (dedup)

    @property
    def storage_key(self) -> str:
        return blob_key(self.sha256)


def _store() -> ObjectStore:
    return blobs_store()


def blob_path(sha256: str) -> Path | None:
    """Local filesystem path for a blob, or ``None`` on a remote backend."""
    return _store().local_path(blob_key(sha256))


def blob_exists(sha256: str) -> bool:
    return _store().exists(blob_key(sha256))


def open_blob(sha256: str) -> BinaryIO:
    return _store().open(blob_key(sha256))


def delete_blob(sha256: str) -> None:
    _store().delete(blob_key(sha256))


def store_stream(src: BinaryIO) -> BlobInfo:
    """Hash ``src`` while spooling it to a temp file, then place it once."""
    store = _store()
    hasher = hashlib.sha256()
    size = 0
    fd, tmp_name = tempfile.mkstemp(prefix="dc-blob-")
    tmp: Path | None = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as buf:
            while chunk := src.read(_CHUNK):
                hasher.update(chunk)
                size += len(chunk)
                buf.write(chunk)
        digest = hasher.hexdigest()
        key = blob_key(digest)
        if store.exists(key):
            return BlobInfo(digest, size, created=False)
        store.put_file(key, tmp)  # consumes tmp
        tmp = None
        return BlobInfo(digest, size, created=True)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def store_bytes(data: bytes) -> BlobInfo:
    return store_stream(io.BytesIO(data))


def list_blob_hashes() -> list[str]:
    """Every blob currently stored, by hash. Used by the cleanup sweep."""
    out: list[str] = []
    for key in _store().iter_keys():
        name = key.rsplit("/", 1)[-1]
        if len(name) == 64:
            out.append(name)
    return out
