"""Cached image thumbnails for previews (web + bot)."""

from __future__ import annotations

from pathlib import Path

from starlette.concurrency import run_in_threadpool

from app import storage

IMAGE_MIMES = {
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "image/bmp",
    "image/x-ms-bmp",
    "image/tiff",
}
IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "tif", "tiff"}


def can_thumb(mime: str, ext: str) -> bool:
    return mime in IMAGE_MIMES or (ext or "").lower().lstrip(".") in IMAGE_EXTS


def thumb_path(sha256: str) -> Path:
    return storage.derived_dir(sha256) / "thumb.webp"


def _render(src: Path, dst: Path, max_px: int) -> None:
    from PIL import Image, ImageOps

    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im)
        im.thumbnail((max_px, max_px))
        if im.mode not in ("RGB", "RGBA", "L"):
            im = im.convert("RGB")
        im.save(dst, "WEBP", quality=80, method=4)


async def ensure_thumb(sha256: str, *, max_px: int = 512) -> Path | None:
    """Return the cached thumbnail path, rendering it once. ``None`` if it can't."""
    dst = thumb_path(sha256)
    if dst.is_file():
        return dst
    try:
        with storage.blobs_store().open_local(storage.blob_key(sha256)) as src:
            await run_in_threadpool(_render, src, dst, max_px)
    except Exception:  # corrupt / unsupported image, or the blob is gone
        return None
    return dst
