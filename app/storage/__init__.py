"""Pluggable file storage.

``from app import storage`` keeps working exactly as before: the blob helpers
(:func:`store_stream`, :func:`blob_path`, …) and the cache-path helpers
(:func:`derived_dir`, :func:`set_archive_path`, …) are re-exported here.

New code should reach for the backend directly —
:func:`blobs_store` / :func:`derived_store` / :func:`artifacts_store` return an
:class:`ObjectStore` — so it stays backend-agnostic.
"""

from __future__ import annotations

from app.storage.base import ObjectNotFound, ObjectStore
from app.storage.blobs import (
    BlobInfo,
    blob_exists,
    blob_key,
    blob_path,
    delete_blob,
    list_blob_hashes,
    open_blob,
    store_bytes,
    store_stream,
)
from app.storage.layout import (
    artifact_path,
    artifacts_dir,
    derived_dir,
    remove_derived,
    set_archive_name,
    set_archive_path,
)
from app.storage.registry import artifacts_store, blobs_store, derived_store

__all__ = [
    "BlobInfo",
    "ObjectNotFound",
    "ObjectStore",
    "artifact_path",
    "artifacts_dir",
    "artifacts_store",
    "blob_exists",
    "blob_key",
    "blob_path",
    "blobs_store",
    "delete_blob",
    "derived_dir",
    "derived_store",
    "list_blob_hashes",
    "open_blob",
    "remove_derived",
    "set_archive_name",
    "set_archive_path",
    "store_bytes",
    "store_stream",
]
