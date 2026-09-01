"""Best-effort file metadata: mime type, extension, and the document's own date.

``doc_date`` is read from PDF info and Office core properties. Anything else
(or a parse failure) yields ``None`` and the user sets the date by hand.
"""

from __future__ import annotations

import mimetypes
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree as ET

mimetypes.add_type("application/x-7z-compressed", ".7z")
mimetypes.add_type("application/vnd.rar", ".rar")
mimetypes.add_type("text/markdown", ".md")

_MAGIC: list[tuple[bytes, str]] = [
    (b"%PDF-", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF8", "image/gif"),
    (b"7z\xbc\xaf\x27\x1c", "application/x-7z-compressed"),
    (b"Rar!\x1a\x07", "application/vnd.rar"),
    (b"\x1f\x8b", "application/gzip"),
]

_OOXML = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


@dataclass(frozen=True)
class FileMeta:
    mime: str
    ext: str
    doc_date: datetime | None


def _sniff_mime(head: bytes, name: str) -> str:
    for sig, mime in _MAGIC:
        if head.startswith(sig):
            return mime
    if head[:2] == b"PK":  # zip container — could be an OOXML doc
        guess, _ = mimetypes.guess_type(name)
        if guess in _OOXML:
            return guess
        return "application/zip"
    guess, _ = mimetypes.guess_type(name)
    return guess or "application/octet-stream"


def _parse_pdf_date(raw: str) -> datetime | None:
    # "D:20240115103000+03'00'"
    raw = raw.strip().removeprefix("D:")
    if len(raw) < 8 or not raw[:8].isdigit():
        return None
    try:
        dt = datetime.strptime(raw[:14].ljust(14, "0"), "%Y%m%d%H%M%S")
    except ValueError:
        return None
    tz = raw[14:]
    if tz and tz[0] in "+-" and len(tz) >= 3 and tz[1:3].isdigit():
        offset = int(tz[1:3])
        sign = 1 if tz[0] == "+" else -1
        from datetime import timedelta, timezone

        return dt.replace(tzinfo=timezone(sign * timedelta(hours=offset)))
    return dt.replace(tzinfo=UTC)


def _pdf_date(path: Path) -> datetime | None:
    try:
        from pypdf import PdfReader

        info = PdfReader(str(path)).metadata
        raw = info.get("/CreationDate") if info else None
        return _parse_pdf_date(str(raw)) if raw else None
    except Exception:
        return None


def _ooxml_date(path: Path) -> datetime | None:
    try:
        with zipfile.ZipFile(path) as zf:
            core = zf.read("docProps/core.xml")
        root = ET.fromstring(core)
        ns = {"dcterms": "http://purl.org/dc/terms/"}
        el = root.find("dcterms:created", ns)
        if el is None or not el.text:
            return None
        text = el.text.strip().replace("Z", "+00:00")
        return datetime.fromisoformat(text)
    except Exception:
        return None


def extract(path: Path, original_name: str) -> FileMeta:
    head = path.open("rb").read(64)
    mime = _sniff_mime(head, original_name)
    ext = Path(original_name).suffix.lower().lstrip(".")[:32]

    doc_date: datetime | None = None
    if mime == "application/pdf":
        doc_date = _pdf_date(path)
    elif mime in _OOXML:
        doc_date = _ooxml_date(path)

    return FileMeta(mime=mime, ext=ext, doc_date=doc_date)
