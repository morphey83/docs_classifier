"""The S3 backend, exercised against an in-process S3 fake (moto)."""

from __future__ import annotations

import io

import pytest

moto = pytest.importorskip("moto")

from app.storage.base import ObjectNotFound  # noqa: E402
from app.storage.s3 import S3ObjectStore, _client  # noqa: E402

BUCKET = "dc-test"


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    _client.cache_clear()
    with moto.mock_aws():
        import boto3

        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
        yield S3ObjectStore(
            bucket=BUCKET,
            prefix="blobs",
            endpoint=None,
            region="us-east-1",
            access_key="test",
            secret_key="test",
            presign_ttl=120,
        )
    _client.cache_clear()


def test_put_get_roundtrip(store):
    n = store.put("ab/cd/x", io.BytesIO(b"hello s3"))
    assert n == 8
    assert store.exists("ab/cd/x")
    assert store.size("ab/cd/x") == 8
    with store.open("ab/cd/x") as body:
        assert body.read() == b"hello s3"
    assert b"".join(store.stream("ab/cd/x", chunk=3)) == b"hello s3"


def test_missing_key(store):
    assert store.exists("nope") is False
    assert store.size("nope") is None
    with pytest.raises(ObjectNotFound):
        store.open("nope")


def test_delete_then_gone(store):
    store.put_bytes("k", b"v")
    store.delete("k")
    assert not store.exists("k")


def test_open_local_copies_and_cleans_up(store):
    store.put_bytes("doc/1", b"payload")
    with store.open_local("doc/1") as path:
        assert path.read_bytes() == b"payload"
        tmp = path
    assert not tmp.exists()  # temp copy removed on exit


def test_put_file_uploads_and_consumes(store, tmp_path):
    src = tmp_path / "up.bin"
    src.write_bytes(b"12345")
    assert store.put_file("box/f", src) == 5
    assert not src.exists()
    with store.open("box/f") as body:
        assert body.read() == b"12345"


def test_iter_keys_strips_prefix(store):
    store.put_bytes("aa/bb/one", b"1")
    store.put_bytes("aa/cc/two", b"2")
    assert sorted(store.iter_keys()) == ["aa/bb/one", "aa/cc/two"]


def test_presigned_url_points_at_the_object(store):
    store.put_bytes("ab/cd/e", b"x")
    url = store.presigned_url("ab/cd/e", filename="report.pdf")
    assert "blobs/ab/cd/e" in url
    assert "X-Amz-Signature" in url


async def test_upload_and_download_through_s3_backend(monkeypatch, alice, tmp_path):
    """End-to-end: the app configured for S3 stores and serves a real upload."""
    from app.config import settings
    from tests.conftest import web_csrf

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setattr(settings, "storage_blobs", "s3")
    monkeypatch.setattr(settings, "s3_bucket", BUCKET)
    monkeypatch.setattr(settings, "s3_access_key", "test")
    monkeypatch.setattr(settings, "s3_secret_key", "test")
    monkeypatch.setattr(settings, "s3_presign", False)  # stream through the app
    _client.cache_clear()

    with moto.mock_aws():
        import boto3

        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)

        page = (await alice.get("/")).text
        r = await alice.post("/domains", data={"name": "S3", "csrf_token": web_csrf(page)})
        slug = r.headers["location"].rsplit("/", 1)[-1]

        up = (await alice.get(f"/upload?domain={slug}")).text
        body = b"content that lives in object storage"
        r = await alice.post(
            "/upload",
            data={"domain": slug, "csrf_token": web_csrf(up)},
            files={"file": ("s3doc.txt", body, "text/plain")},
        )
        assert r.status_code == 200, r.text[:300]

        import re

        page = (await alice.get(f"/search?domain={slug}", headers={"HX-Request": "true"})).text
        doc_id = re.search(r"/documents/([0-9a-f-]{36})", page).group(1)

        got = await alice.get(f"/documents/{doc_id}/download")
        assert got.status_code == 200
        assert got.content == body

        # the bytes really went to S3, not the local disk
        listing = boto3.client("s3", region_name="us-east-1").list_objects_v2(Bucket=BUCKET)
        assert any(k["Key"].startswith("blobs/") for k in listing.get("Contents", []))

    _client.cache_clear()


async def test_download_redirects_to_a_presigned_url(monkeypatch, alice):
    from app.config import settings
    from tests.conftest import web_csrf

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setattr(settings, "storage_blobs", "s3")
    monkeypatch.setattr(settings, "s3_bucket", BUCKET)
    monkeypatch.setattr(settings, "s3_access_key", "test")
    monkeypatch.setattr(settings, "s3_secret_key", "test")
    monkeypatch.setattr(settings, "s3_presign", True)
    _client.cache_clear()

    with moto.mock_aws():
        import re

        import boto3

        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
        page = (await alice.get("/")).text
        r = await alice.post("/domains", data={"name": "P", "csrf_token": web_csrf(page)})
        slug = r.headers["location"].rsplit("/", 1)[-1]
        up = (await alice.get(f"/upload?domain={slug}")).text
        await alice.post(
            "/upload",
            data={"domain": slug, "csrf_token": web_csrf(up)},
            files={"file": ("p.txt", b"hi", "text/plain")},
        )
        sp = (await alice.get(f"/search?domain={slug}", headers={"HX-Request": "true"})).text
        doc_id = re.search(r"/documents/([0-9a-f-]{36})", sp).group(1)

        got = await alice.get(f"/documents/{doc_id}/download", follow_redirects=False)
        assert got.status_code in (302, 307)
        assert "X-Amz-Signature" in got.headers["location"]

    _client.cache_clear()


def test_migrate_local_to_s3(monkeypatch, tmp_path):
    from app.config import settings
    from app.storage import migrate
    from app.storage.local import LocalObjectStore

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "s3_bucket", BUCKET)
    monkeypatch.setattr(settings, "s3_access_key", "test")
    monkeypatch.setattr(settings, "s3_secret_key", "test")
    monkeypatch.setattr(settings, "s3_prefix", "")
    _client.cache_clear()

    src = LocalObjectStore(tmp_path / "data" / "blobs")
    src.put_bytes("ab/cd/one", b"first")
    src.put_bytes("ef/01/two", b"second")

    with moto.mock_aws():
        import boto3

        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)

        migrate.run("s3", commit=False, delete_source=False)  # dry run — no writes
        dest = migrate._backend("s3")
        assert not dest.exists("ab/cd/one")

        migrate.run("s3", commit=True, delete_source=True)
        with dest.open("ab/cd/one") as fh:
            assert fh.read() == b"first"
        with dest.open("ef/01/two") as fh:
            assert fh.read() == b"second"
        assert not src.exists("ab/cd/one")  # --delete-source removed it

        migrate.run("s3", commit=True, delete_source=False)  # idempotent re-run
    _client.cache_clear()


def test_presign_can_be_disabled():
    _client.cache_clear()
    with moto.mock_aws():
        import boto3

        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
        s = S3ObjectStore(
            bucket=BUCKET, prefix="", endpoint=None, region="us-east-1",
            access_key="test", secret_key="test", presign=False,
        )
        s.put_bytes("k", b"v")
        assert s.presigned_url("k") is None
    _client.cache_clear()
