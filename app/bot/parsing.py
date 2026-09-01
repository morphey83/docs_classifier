"""`/find` mini-syntax parser (§8). Pure — unit-tested."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.models import DocStatus
from app.services.search import SearchFilters

_YES = {"yes", "y", "да", "1", "true", "+"}
_NO = {"no", "n", "нет", "0", "false", "-"}
_STATUS = {s.value for s in DocStatus}


def _tri(value: str) -> bool | None:
    v = value.strip().lower()
    if v in _YES:
        return True
    if v in _NO:
        return False
    return None


@dataclass
class ParsedQuery:
    text: str = ""
    tags: list[str] = field(default_factory=list)
    ext: str | None = None
    has_ocr: bool | None = None
    has_index: bool | None = None
    status: str | None = None
    year: int | None = None
    domain_id: str | None = None  # set by the "refine → domain" button, not the text
    page: int = 0

    def with_page(self, page: int) -> ParsedQuery:
        return ParsedQuery(**{**self.__dict__, "page": max(0, page)})


def parse_query(raw: str) -> ParsedQuery:
    tags: list[str] = []
    words: list[str] = []
    ext = status = None
    has_ocr = has_index = year = None

    for tok in (raw or "").split():
        low = tok.lower()
        if tok.startswith("#") and len(tok) > 1:
            tags.append(tok[1:])
        elif low.startswith("type:"):
            ext = low[5:].lstrip(".") or None
        elif low.startswith("ocr:"):
            has_ocr = _tri(low[4:])
        elif low.startswith("index:"):
            has_index = _tri(low[6:])
        elif low.startswith("status:") and low[7:] in _STATUS:
            status = low[7:]
        elif tok.isdigit() and len(tok) == 4 and 1900 <= int(tok) <= 2100:
            year = int(tok)
        else:
            words.append(tok)

    return ParsedQuery(
        text=" ".join(words),
        tags=tags,
        ext=ext,
        has_ocr=has_ocr,
        has_index=has_index,
        status=status,
        year=year,
    )


def to_filters(pq: ParsedQuery, page_size: int) -> SearchFilters:
    f = SearchFilters(
        q=pq.text or None,
        tags_all=list(pq.tags),
        ext=pq.ext,
        has_ocr=pq.has_ocr,
        has_index=pq.has_index,
        page=pq.page + 1,
        page_size=page_size,
        sort="uploaded_at",
    )
    if pq.status:
        f.status = DocStatus(pq.status)
    if pq.year:
        f.doc_date_from = datetime(pq.year, 1, 1, tzinfo=UTC)
        f.doc_date_to = datetime(pq.year, 12, 31, 23, 59, 59, tzinfo=UTC)
    return f


def describe(pq: ParsedQuery) -> str:
    parts: list[str] = []
    if pq.text:
        parts.append(f"«{pq.text}»")
    parts += [f"#{t}" for t in pq.tags]
    if pq.ext:
        parts.append(f"тип:{pq.ext}")
    if pq.year:
        parts.append(str(pq.year))
    if pq.status:
        parts.append(f"статус:{pq.status}")
    if pq.has_ocr is not None:
        parts.append("распознан" if pq.has_ocr else "не распознан")
    if pq.has_index is not None:
        parts.append("проиндексирован" if pq.has_index else "не проиндексирован")
    return " · ".join(parts) or "всё"
