"""OCR engine wrapper.

Heavy, native-dependency-laden libraries (``ocrmypdf``, ``pytesseract``,
``Pillow``) are imported lazily inside the functions so the module loads on a
box without them. Tests monkeypatch :func:`run_ocr`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from app.config import settings

IMAGE_MIMES = {
    "image/png",
    "image/jpeg",
    "image/tiff",
    "image/webp",
    "image/bmp",
    "image/gif",
}


def is_supported(mime: str) -> bool:
    return mime == "application/pdf" or mime in IMAGE_MIMES


@dataclass
class OcrResult:
    text: str
    sidecar_pdf: bytes | None = None


def run_ocr(path: Path, mime: str, lang: str) -> OcrResult:
    """Blocking. Call via ``run_in_threadpool``."""
    os.environ.setdefault("OMP_THREAD_LIMIT", "1")
    if mime == "application/pdf":
        return _ocr_pdf(path, lang)
    return OcrResult(text=_ocr_image(path, lang))


def _ocr_pdf(path: Path, lang: str) -> OcrResult:
    import tempfile

    import ocrmypdf

    with tempfile.TemporaryDirectory() as td:
        out_pdf = Path(td) / "out.pdf"
        sidecar = Path(td) / "out.txt"
        try:
            ocrmypdf.ocr(
                str(path),
                str(out_pdf),
                language=lang.replace("+", "+"),
                sidecar=str(sidecar),
                skip_text=True,
                deskew=True,
                rotate_pages=True,
                jobs=1,
                progress_bar=False,
            )
        except ocrmypdf.exceptions.PriorOcrFoundError:
            return OcrResult(text="")
        text = sidecar.read_text(encoding="utf-8", errors="replace") if sidecar.exists() else ""
        pdf_bytes = out_pdf.read_bytes() if out_pdf.exists() else None
    return OcrResult(text=text, sidecar_pdf=pdf_bytes)


def _ocr_image(path: Path, lang: str) -> str:
    import pytesseract
    from PIL import Image

    with Image.open(path) as img:
        img = img.convert("RGB")
        cap = settings.ocr_image_max_px
        if max(img.size) > cap:
            ratio = cap / max(img.size)
            img = img.resize((int(img.width * ratio), int(img.height * ratio)))
        return pytesseract.image_to_string(img, lang=lang)
