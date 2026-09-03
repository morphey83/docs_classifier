"""The local-filesystem storage backend — one directory tree per store."""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

from app.storage.base import ObjectNotFound, ObjectStore

_CHUNK = 1 << 20


class LocalObjectStore(ObjectStore):
    """Keys map to files under ``root``. ``root`` need not exist yet."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"LocalObjectStore({self._root})"

    def _full(self, key: str) -> Path:
        # keys are '/'-separated relative POSIX paths; reject traversal
        rel = Path(key.strip("/"))
        if rel.is_absolute() or ".." in rel.parts:
            raise ValueError(f"unsafe storage key: {key!r}")
        return self._root / rel

    # --- reads ----------------------------------------------------------
    def open(self, key: str) -> BinaryIO:
        try:
            return self._full(key).open("rb")
        except FileNotFoundError as err:
            raise ObjectNotFound(key) from err

    def exists(self, key: str) -> bool:
        return self._full(key).is_file()

    def size(self, key: str) -> int | None:
        try:
            return self._full(key).stat().st_size
        except FileNotFoundError:
            return None

    def local_path(self, key: str) -> Path:
        return self._full(key)

    def iter_keys(self, prefix: str = "") -> Iterator[str]:
        base = self._full(prefix) if prefix else self._root
        if not base.is_dir():
            return
        for path in base.rglob("*"):
            if path.is_file() and not path.name.startswith(".incoming-"):
                yield path.relative_to(self._root).as_posix()

    # --- writes -------------------------------------------------------
    def put(self, key: str, src: BinaryIO) -> int:
        dest = self._full(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        size = 0
        fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=".incoming-")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as out:
                while chunk := src.read(_CHUNK):
                    size += len(chunk)
                    out.write(chunk)
            _place(tmp, dest)
            return size
        finally:
            tmp.unlink(missing_ok=True)

    def put_file(self, key: str, path: Path) -> int:
        dest = self._full(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        size = path.stat().st_size
        _place(path, dest)
        return size

    def delete(self, key: str) -> None:
        self._full(key).unlink(missing_ok=True)


def _place(src: Path, dest: Path) -> None:
    """Atomically move ``src`` onto ``dest`` (rename, else cross-device copy)."""
    try:
        os.replace(src, dest)
    except OSError:
        shutil.move(str(src), str(dest))
