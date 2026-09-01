"""Message text builders for the bot. Kept pure for easy testing."""

from __future__ import annotations

from collections.abc import Sequence

from app.models import Document

_UNITS = ("Б", "КБ", "МБ", "ГБ")


def human_size(n: int) -> str:
    size = float(n)
    for unit in _UNITS:
        if size < 1024 or unit == _UNITS[-1]:
            return f"{size:.0f} {unit}" if unit == "Б" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} Б"


def _short_type(mime: str, ext: str) -> str:
    if ext:
        return ext.upper()
    return (mime.split("/")[-1] or "файл").upper()


def result_line(doc: Document, domain_name: str | None, tag_names: Sequence[str]) -> str:
    date = doc.doc_date.date().isoformat() if doc.doc_date else "—"
    head = f"[{domain_name or '?'}] {doc.title}"
    meta = f"{_short_type(doc.mime, doc.ext)} · {date} · {human_size(doc.size_bytes)}"
    line = f"{head}\n{meta}"
    if tag_names:
        line += "\n🔖 " + ", ".join(sorted(tag_names))
    badges = []
    if doc.ocr_at is not None:
        badges.append("распознан")
    if doc.indexed_at is not None:
        badges.append("проиндексирован")
    if badges:
        line += "\n· " + " · ".join(badges)
    return line


def set_line(name: str, domain_name: str, count: int) -> str:
    return f"{name} — {domain_name} · {count} док."
