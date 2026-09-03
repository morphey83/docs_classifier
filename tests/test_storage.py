"""The storage backend contract + the content-addressing layer on top."""

from __future__ import annotations

import io

import pytest

from app import storage
from app.storage.base import ObjectNotFound
from app.storage.local import LocalObjectStore


@pytest.fixture
def store(tmp_path):
    return LocalObjectStore(tmp_path / "s")


def test_put_get_roundtrip(store):
    n = store.put("ab/cd/thing", io.BytesIO(b"hello"))
    assert n == 5
    assert store.exists("ab/cd/thing")
    assert store.size("ab/cd/thing") == 5
    with store.open("ab/cd/thing") as fh:
        assert fh.read() == b"hello"


def test_open_missing_raises(store):
    with pytest.raises(ObjectNotFound):
        store.open("nope")
    assert store.size("nope") is None
    assert store.exists("nope") is False


def test_delete_is_idempotent(store):
    store.put_bytes("k", b"x")
    store.delete("k")
    store.delete("k")  # missing key -> no error
    assert not store.exists("k")


def test_put_file_consumes_source(store, tmp_path):
    src = tmp_path / "incoming.bin"
    src.write_bytes(b"payload")
    store.put_file("box/f", src)
    assert not src.exists()
    with store.open("box/f") as fh:
        assert fh.read() == b"payload"


def test_iter_keys_skips_incoming_and_returns_posix(store):
    store.put_bytes("a/b/one", b"1")
    store.put_bytes("a/c/two", b"2")
    assert sorted(store.iter_keys()) == ["a/b/one", "a/c/two"]
    assert list(store.iter_keys("a/b")) == ["a/b/one"]


def test_key_traversal_is_rejected(store):
    with pytest.raises(ValueError):
        store.open("../escape")
    with pytest.raises(ValueError):
        store.put("a/../../b", io.BytesIO(b"x"))


def test_blob_layer_dedups():
    # the autouse ``_data_dir`` fixture points settings.data_dir at a tmp tree
    first = storage.store_bytes(b"same content")
    second = storage.store_bytes(b"same content")
    assert first.sha256 == second.sha256
    assert first.created is True
    assert second.created is False
    assert storage.blob_exists(first.sha256)
    assert storage.blob_path(first.sha256).read_bytes() == b"same content"
    assert storage.list_blob_hashes() == [first.sha256]
    storage.delete_blob(first.sha256)
    assert not storage.blob_exists(first.sha256)


def test_blob_storage_key_layout():
    info = storage.BlobInfo("abcdef0123456789" * 4, 10, created=True)
    assert info.storage_key == "ab/cd/" + info.sha256
