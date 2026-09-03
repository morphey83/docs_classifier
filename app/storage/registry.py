"""Which backend serves each class of stored data.

Three classes, potentially on different backends (see ``docs/architecture.md``):

* **blobs** — content-addressed originals; durable, must never be lost.
* **derived** — per-blob generated files (thumbnails, OCR sidecars); a cache.
* **artifacts** — export / set-archive zips; a cache.

Phase 1: all three are :class:`LocalObjectStore` trees under ``DATA_DIR``.
A later phase reads ``settings.storage_*`` to point ``blobs`` at S3 while the
regenerable caches stay local. Instances are cheap to build and ``settings``
may change under tests, so nothing is cached here.
"""

from __future__ import annotations

from app.config import settings
from app.storage.base import ObjectStore
from app.storage.local import LocalObjectStore


def _build(kind: str) -> ObjectStore:
    return LocalObjectStore(settings.data_dir / kind)


def blobs_store() -> ObjectStore:
    return _build("blobs")


def derived_store() -> ObjectStore:
    return _build("derived")


def artifacts_store() -> ObjectStore:
    return _build("artifacts")
