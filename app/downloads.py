"""Serve a stored blob over HTTP, backend-agnostically.

Local backend → :class:`FileResponse` (range requests, ETag, sendfile).
Remote backend → a presigned-URL redirect when the backend offers one,
else a :class:`StreamingResponse` proxied through this process.
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import HTTPException, status
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse

from app import storage
from app.models import Document

_MISSING = "файл отсутствует в хранилище"


def blob_download(doc: Document) -> Response:
    store = storage.blobs_store()
    key = storage.blob_key(doc.sha256)

    local = store.local_path(key)
    if local is not None:
        if not local.is_file():
            raise HTTPException(status.HTTP_410_GONE, _MISSING)
        return FileResponse(local, media_type=doc.mime, filename=doc.original_name)

    presigned = getattr(store, "presigned_url", None)
    if presigned is not None:
        url = presigned(key, filename=doc.original_name)
        if url:
            return RedirectResponse(url)

    if not store.exists(key):
        raise HTTPException(status.HTTP_410_GONE, _MISSING)
    disposition = f"attachment; filename*=UTF-8''{quote(doc.original_name)}"
    return StreamingResponse(
        store.stream(key),
        media_type=doc.mime,
        headers={
            "Content-Disposition": disposition,
            "Content-Length": str(doc.size_bytes),
        },
    )
