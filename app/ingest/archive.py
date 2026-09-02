"""Safe archive extraction.

Yields ``(entry_name, temp_path)`` pairs for regular files inside an archive.
Guards against zip bombs (entry count, total uncompressed size), path traversal,
and oversized entries. Nested archives are recursed up to ``max_depth``.

Formats: zip + tar(.gz/.bz2/.xz) via the stdlib, 7z via ``py7zr``, rar via
``rarfile`` (needs an ``unar`` / ``unrar`` / ``bsdtar`` binary — Docker only).
"""

from __future__ import annotations

import os
import tarfile
import tempfile
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

ARCHIVE_MIMES = {
    "application/zip": "zip",
    "application/x-7z-compressed": "7z",
    "application/vnd.rar": "rar",
    "application/x-rar-compressed": "rar",
    "application/x-tar": "tar",
    "application/gzip": "tar",
    "application/x-bzip2": "tar",
    "application/x-xz": "tar",
}
ARCHIVE_EXTS = {
    "zip": "zip",
    "7z": "7z",
    "rar": "rar",
    "tar": "tar",
    "gz": "tar",
    "tgz": "tar",
    "bz2": "tar",
    "xz": "tar",
}


def kind_of(mime: str, ext: str) -> str | None:
    return ARCHIVE_MIMES.get(mime) or ARCHIVE_EXTS.get(ext)


class ArchiveError(Exception):
    pass


class ArchiveBomb(ArchiveError):
    pass


class UnsupportedArchive(ArchiveError):
    pass


@dataclass
class Limits:
    max_entries: int
    max_total_bytes: int
    max_entry_bytes: int
    max_depth: int


def _safe_name(name: str) -> str | None:
    p = PurePosixPath(name.replace("\\", "/"))
    if p.is_absolute() or any(part in ("..", "") for part in p.parts):
        return None
    return str(p)


class _Counter:
    def __init__(self, limits: Limits) -> None:
        self.limits = limits
        self.entries = 0
        self.total = 0

    def add(self, name: str, size: int) -> None:
        self.entries += 1
        self.total += size
        if self.entries > self.limits.max_entries:
            raise ArchiveBomb(f"archive has more than {self.limits.max_entries} files")
        if self.total > self.limits.max_total_bytes:
            raise ArchiveBomb("archive unpacks to more than the allowed size")
        if size > self.limits.max_entry_bytes:
            raise ArchiveBomb(f"'{name}' is larger than the per-file limit")


def iter_archive(path: Path, kind: str, limits: Limits) -> Iterator[tuple[str, Path]]:
    yield from _iter(path, kind, limits, _Counter(limits), depth=0, prefix="")


def _iter(
    path: Path, kind: str, limits: Limits, counter: _Counter, *, depth: int, prefix: str
) -> Iterator[tuple[str, Path]]:
    for name, tmp in _extract_flat(path, kind, limits, counter):
        full = f"{prefix}{name}"
        sub_kind = _nested_kind(name)
        if sub_kind and depth < limits.max_depth:
            try:
                yield from _iter(tmp, sub_kind, limits, counter, depth=depth + 1, prefix=f"{full}/")
            finally:
                tmp.unlink(missing_ok=True)
        else:
            yield full, tmp


def _nested_kind(name: str) -> str | None:
    ext = Path(name).suffix.lower().lstrip(".")
    return ARCHIVE_EXTS.get(ext)


def _tmp() -> Path:
    fd, name = tempfile.mkstemp(prefix="dc-arc-")
    os.close(fd)
    return Path(name)


def _extract_flat(
    path: Path, kind: str, limits: Limits, counter: _Counter
) -> Iterator[tuple[str, Path]]:
    if kind == "zip":
        yield from _zip(path, limits, counter)
    elif kind == "tar":
        yield from _tar(path, limits, counter)
    elif kind == "7z":
        yield from _sevenz(path, limits, counter)
    elif kind == "rar":
        yield from _rar(path, limits, counter)
    else:  # pragma: no cover
        raise UnsupportedArchive(kind)


def _zip(path: Path, limits: Limits, counter: _Counter) -> Iterator[tuple[str, Path]]:
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            safe = _safe_name(info.filename)
            if safe is None:
                continue
            counter.add(safe, info.file_size)
            out = _tmp()
            with zf.open(info) as src, out.open("wb") as dst:
                _copy(src, dst, limits.max_entry_bytes)
            yield safe, out


def _tar(path: Path, limits: Limits, counter: _Counter) -> Iterator[tuple[str, Path]]:
    with tarfile.open(path) as tf:
        for member in tf:
            if not member.isfile():
                continue
            safe = _safe_name(member.name)
            if safe is None:
                continue
            counter.add(safe, member.size)
            src = tf.extractfile(member)
            if src is None:
                continue
            out = _tmp()
            with src, out.open("wb") as dst:
                _copy(src, dst, limits.max_entry_bytes)
            yield safe, out


def _sevenz(path: Path, limits: Limits, counter: _Counter) -> Iterator[tuple[str, Path]]:
    try:
        import py7zr
    except ImportError as err:  # pragma: no cover
        raise UnsupportedArchive("на сервере не установлена поддержка 7z (py7zr)") from err

    # Extract to a temp dir on disk (py7zr 1.x has no streaming read), checking
    # the declared sizes against the budget *before* extracting anything.
    with tempfile.TemporaryDirectory(prefix="dc-7z-") as td:
        td_path = Path(td)
        wanted: list[str] = []
        with py7zr.SevenZipFile(path, "r") as zf:
            for info in zf.list():
                if info.is_directory:
                    continue
                safe = _safe_name(info.filename)
                if safe is None:
                    continue
                counter.add(safe, int(info.uncompressed or 0))
                wanted.append(info.filename)
            if not wanted:
                return
            zf.reset()
            zf.extract(path=td, targets=wanted)

        for original in wanted:
            safe = _safe_name(original)
            src = td_path / original
            if safe is None or not src.is_file():
                continue
            out = _tmp()
            with src.open("rb") as s, out.open("wb") as d:
                _copy(s, d, limits.max_entry_bytes)
            yield safe, out


def _rar(path: Path, limits: Limits, counter: _Counter) -> Iterator[tuple[str, Path]]:
    try:
        import rarfile
    except ImportError as err:  # pragma: no cover
        raise UnsupportedArchive("rar support is not installed") from err
    try:
        rf = rarfile.RarFile(path)
    except rarfile.RarCannotExec as err:
        raise UnsupportedArchive("rar extraction needs 'unar' or 'unrar' on the server") from err
    with rf:
        for info in rf.infolist():
            if info.is_dir():
                continue
            safe = _safe_name(info.filename)
            if safe is None:
                continue
            counter.add(safe, info.file_size)
            out = _tmp()
            with rf.open(info) as src, out.open("wb") as dst:
                _copy(src, dst, limits.max_entry_bytes)
            yield safe, out


def _copy(src, dst, cap: int) -> None:
    written = 0
    while chunk := src.read(1 << 20):
        written += len(chunk)
        if written > cap:
            raise ArchiveBomb("entry exceeds the per-file limit while streaming")
        dst.write(chunk)
