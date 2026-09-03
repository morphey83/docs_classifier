"""The storage backend contract.

An :class:`ObjectStore` is a flat ``key -> bytes`` store. Keys are
``"/"``-separated relative paths (e.g. ``"ab/cd/<sha256>"``). Content
addressing, dedup and the blob / derived / artifact layout live *above* this
interface (see :mod:`app.storage.blobs` and :mod:`app.storage.layout`); a
backend only moves opaque bytes.

Phase 1 ships a single backend, :class:`app.storage.local.LocalObjectStore`.
Remote backends (S3, …) implement the same ABC; the one method they cannot
answer is :meth:`~ObjectStore.local_path`, which lets subprocess-bound callers
(OCR, thumbnails, zip writers) keep using a real filesystem path when one
exists. Phase 2 adds a ``open_local()`` context manager that copies a remote
object to a temp file for those callers.
"""

from __future__ import annotations

import abc
import io
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO


class ObjectNotFound(KeyError):
    """Raised by :meth:`ObjectStore.open` when a key is absent."""


class ObjectStore(abc.ABC):
    """A flat key -> bytes store. See the module docstring."""

    # --- reads -----------------------------------------------------------
    @abc.abstractmethod
    def open(self, key: str) -> BinaryIO:
        """Open ``key`` for streaming binary reads. Raises :class:`ObjectNotFound`."""

    @abc.abstractmethod
    def exists(self, key: str) -> bool:
        """Whether ``key`` currently holds an object."""

    @abc.abstractmethod
    def size(self, key: str) -> int | None:
        """Byte size of ``key``, or ``None`` if it is absent."""

    @abc.abstractmethod
    def local_path(self, key: str) -> Path | None:
        """A real filesystem path for ``key``, or ``None`` for remote backends.

        The local backend returns the path whether or not the file exists yet
        (callers may create it). Remote backends return ``None`` and callers
        must fall back to :meth:`open` (or, from phase 2, ``open_local()``).
        """

    @abc.abstractmethod
    def iter_keys(self, prefix: str = "") -> Iterator[str]:
        """Every key at or below ``prefix``. Used by the GC sweep."""

    # --- writes --------------------------------------------------------
    @abc.abstractmethod
    def put(self, key: str, src: BinaryIO) -> int:
        """Stream ``src`` into ``key``, overwriting. Returns the byte count."""

    @abc.abstractmethod
    def delete(self, key: str) -> None:
        """Remove ``key``. A missing key is not an error."""

    # --- provided helpers -------------------------------------------------
    def put_bytes(self, key: str, data: bytes) -> int:
        return self.put(key, io.BytesIO(data))

    def put_file(self, key: str, path: Path) -> int:
        """Move an existing local file into ``key``, consuming the source.

        The local backend renames it (atomic on the same filesystem); other
        backends upload then unlink. Either way ``path`` no longer exists on
        return.
        """
        with path.open("rb") as fh:
            size = self.put(key, fh)
        path.unlink(missing_ok=True)
        return size
