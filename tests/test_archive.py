"""Direct coverage for app/ingest/archive.py — zip + 7z extraction & guards."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from app.ingest.archive import ArchiveBomb, Limits, iter_archive, kind_of

LIMITS = Limits(max_entries=100, max_total_bytes=10_000_000, max_entry_bytes=5_000_000, max_depth=1)


def _write_zip(p: Path, files: dict[str, bytes]) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    p.write_bytes(buf.getvalue())


def test_zip_yields_regular_files(tmp_path):
    arc = tmp_path / "a.zip"
    _write_zip(arc, {"a.txt": b"alpha", "sub/b.txt": b"bravo", "sub/": b""})
    got = {name: path.read_bytes() for name, path in iter_archive(arc, "zip", LIMITS)}
    assert got == {"a.txt": b"alpha", "sub/b.txt": b"bravo"}


def test_zip_bomb_is_rejected(tmp_path):
    arc = tmp_path / "boom.zip"
    _write_zip(arc, {"big.txt": b"x" * 6_000_000})  # over max_entry_bytes
    with pytest.raises(ArchiveBomb):
        list(iter_archive(arc, "zip", LIMITS))


def test_kind_detection():
    assert kind_of("application/x-7z-compressed", "") == "7z"
    assert kind_of("", "7z") == "7z"
    assert kind_of("application/zip", "") == "zip"
    assert kind_of("text/plain", "txt") is None


def test_7z_extraction(tmp_path):
    py7zr = pytest.importorskip("py7zr")
    arc = tmp_path / "a.7z"
    with py7zr.SevenZipFile(arc, "w") as zf:
        zf.writestr(b"hello", "one.txt")
        zf.writestr(b"x" * 200, "nested/two.txt")
    got = {name: path.read_bytes() for name, path in iter_archive(arc, "7z", LIMITS)}
    assert got == {"one.txt": b"hello", "nested/two.txt": b"x" * 200}


def test_7z_bomb_is_rejected_before_extraction(tmp_path):
    py7zr = pytest.importorskip("py7zr")
    arc = tmp_path / "boom.7z"
    with py7zr.SevenZipFile(arc, "w") as zf:
        zf.writestr(b"y" * 6_000_000, "huge.txt")
    with pytest.raises(ArchiveBomb):
        list(iter_archive(arc, "7z", LIMITS))
