"""Which backend serves each class of stored data.

Three classes, potentially on different backends (see ``docs/architecture.md``):

* **blobs** — content-addressed originals; durable, must never be lost.
  Backend chosen by ``STORAGE_BLOBS`` (``local`` | ``s3``).
* **derived** — per-blob generated files (thumbnails, OCR sidecars); a cache.
  Always local.
* **artifacts** — export / set-archive zips; a cache. Always local.

Instances are cheap (the S3 client itself is cached in :mod:`app.storage.s3`),
and ``settings`` may change under tests, so nothing is cached here.
"""

from __future__ import annotations

from app.config import settings
from app.storage.base import ObjectStore
from app.storage.local import LocalObjectStore


def _local(kind: str) -> LocalObjectStore:
    return LocalObjectStore(settings.data_dir / kind)


def _s3(prefix: str) -> ObjectStore:
    from app.storage.s3 import S3ObjectStore

    return S3ObjectStore(
        bucket=settings.s3_bucket,
        prefix="/".join(p for p in (settings.s3_prefix.strip("/"), prefix) if p),
        endpoint=settings.s3_endpoint,
        region=settings.s3_region,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key,
        addressing=settings.s3_addressing,
        download_endpoint=settings.s3_download_endpoint,
        presign=settings.s3_presign,
        presign_ttl=settings.s3_presign_ttl,
    )


def blobs_store() -> ObjectStore:
    if (settings.storage_blobs or "local").lower() == "s3":
        return _s3("blobs")
    return _local("blobs")


def derived_store() -> ObjectStore:
    return _local("derived")


def artifacts_store() -> ObjectStore:
    return _local("artifacts")
