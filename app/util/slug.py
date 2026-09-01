"""Slug generation. Keeps Unicode letters (incl. Cyrillic); ASCII-only fallback."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable

_SEP = re.compile(r"[\s_]+")
_DROP = re.compile(r"[^\w-]", re.UNICODE)
_DASHES = re.compile(r"-{2,}")


def slugify(text: str, *, max_len: int = 60) -> str:
    text = unicodedata.normalize("NFKC", text).strip().lower()
    text = _SEP.sub("-", text)
    text = _DROP.sub("", text)
    text = _DASHES.sub("-", text).strip("-")
    return text[:max_len].strip("-") or "item"


def unique_slug(base: str, exists: Callable[[str], bool], *, max_len: int = 60) -> str:
    """Return ``slugify(base)`` or ``…-2``/``…-3`` until ``exists`` is False."""
    root = slugify(base, max_len=max_len)
    if not exists(root):
        return root
    n = 2
    while True:
        suffix = f"-{n}"
        candidate = f"{root[: max_len - len(suffix)]}{suffix}"
        if not exists(candidate):
            return candidate
        n += 1
