"""S3-compatible storage backend (boto3, synchronous).

Works against any S3 API — AWS, MinIO, Garage, SeaweedFS. The app never bakes
in a host: point ``S3_ENDPOINT`` at a local MinIO today, at a remote box over
WireGuard tomorrow, and nothing else changes.

boto3 is synchronous; every method here does blocking network I/O, so async
callers must go through :func:`app.storage.aio.fetch_local` or
``run_in_threadpool`` (the blob consumers already do). Clients are cached per
distinct config so we build one, not one per request.
"""

from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path
from typing import Any, BinaryIO

from app.storage.base import ObjectNotFound, ObjectStore


@lru_cache(maxsize=8)
def _client(
    endpoint: str | None,
    region: str,
    access_key: str | None,
    secret_key: str | None,
    addressing: str,
) -> Any:
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=region,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "virtual" if addressing == "virtual" else "path"},
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )


def _is_404(err: Exception) -> bool:
    from botocore.exceptions import ClientError

    return (
        isinstance(err, ClientError)
        and err.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}
    )


class S3ObjectStore(ObjectStore):
    """One bucket + key prefix. Keys are the same ``ab/cd/<sha>`` paths as local."""

    def __init__(
        self,
        *,
        bucket: str,
        prefix: str = "",
        endpoint: str | None,
        region: str,
        access_key: str | None,
        secret_key: str | None,
        addressing: str = "path",
        download_endpoint: str | None = None,
        presign: bool = True,
        presign_ttl: int = 300,
    ) -> None:
        self._bucket = bucket
        self._prefix = prefix.strip("/")
        self._presign = presign
        self._presign_ttl = presign_ttl
        self._client = _client(endpoint, region, access_key, secret_key, addressing)
        self._dl_client = (
            self._client
            if not download_endpoint or download_endpoint == endpoint
            else _client(download_endpoint, region, access_key, secret_key, addressing)
        )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        loc = f"{self._bucket}/{self._prefix}" if self._prefix else self._bucket
        return f"S3ObjectStore({loc})"

    def _obj(self, key: str) -> str:
        rel = key.strip("/")
        if ".." in rel.split("/"):
            raise ValueError(f"unsafe storage key: {key!r}")
        return f"{self._prefix}/{rel}" if self._prefix else rel

    # --- reads ----------------------------------------------------------
    def open(self, key: str) -> BinaryIO:
        try:
            resp = self._client.get_object(Bucket=self._bucket, Key=self._obj(key))
        except Exception as err:
            if _is_404(err):
                raise ObjectNotFound(key) from err
            raise
        return resp["Body"]

    # stream() is inherited from ObjectStore — StreamingBody.read(n) works fine

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=self._obj(key))
            return True
        except Exception as err:
            if _is_404(err):
                return False
            raise

    def size(self, key: str) -> int | None:
        try:
            resp = self._client.head_object(Bucket=self._bucket, Key=self._obj(key))
        except Exception as err:
            if _is_404(err):
                return None
            raise
        return int(resp["ContentLength"])

    def local_path(self, key: str) -> None:
        return None  # remote — callers use open_local() / fetch_local()

    def iter_keys(self, prefix: str = "") -> Iterator[str]:
        paginator = self._client.get_paginator("list_objects_v2")
        base = self._obj(prefix) if prefix else self._prefix
        strip = f"{self._prefix}/" if self._prefix else ""
        for page in paginator.paginate(Bucket=self._bucket, Prefix=base):
            for item in page.get("Contents", []):
                obj_key = item["Key"]
                yield obj_key[len(strip) :] if strip and obj_key.startswith(strip) else obj_key

    def presigned_url(self, key: str, *, filename: str | None = None) -> str | None:
        if not self._presign:
            return None
        params = {"Bucket": self._bucket, "Key": self._obj(key)}
        if filename:
            params["ResponseContentDisposition"] = f'attachment; filename="{filename}"'
        return self._dl_client.generate_presigned_url(
            "get_object", Params=params, ExpiresIn=self._presign_ttl
        )

    # --- writes -------------------------------------------------------
    def put(self, key: str, src: BinaryIO) -> int:
        counter = _Counting(src)
        self._client.upload_fileobj(counter, self._bucket, self._obj(key))
        return counter.total

    def put_file(self, key: str, path: Path) -> int:
        size = path.stat().st_size
        self._client.upload_file(str(path), self._bucket, self._obj(key))
        path.unlink(missing_ok=True)
        return size

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self._bucket, Key=self._obj(key))


class _Counting:
    """Wraps a readable stream, tallying bytes as boto3 consumes it."""

    def __init__(self, inner: BinaryIO) -> None:
        self._inner = inner
        self.total = 0

    def read(self, size: int = -1) -> bytes:
        block = self._inner.read(size)
        self.total += len(block)
        return block
