"""Phase 5 — soft delete / Корзина / restore, force-purge, the cleanup job."""

import uuid
from datetime import timedelta

import pytest_asyncio
from sqlalchemy import select

from app import storage
from app.db import get_sessionmaker
from app.models import Artifact
from app.services.cleanup import run_cleanup
from app.util.time import utcnow


@pytest_asyncio.fixture
async def domain(alice):
    return (await alice.post("/domains", json={"name": "P5"})).json()


async def _upload(client, domain_id, name, body=None):
    body = body if body is not None else name.encode()
    r = await client.post(
        f"/domains/{domain_id}/uploads", files={"file": (name, body, "text/plain")}
    )
    assert r.status_code == 201, r.text
    return r.json()["document"]


async def _download_when_ready(client, url, tries=3):
    for _ in range(tries):
        r = await client.get(url)
        if r.status_code == 200:
            return r
    return r


async def test_soft_delete_hide_then_restore(alice, domain):
    d = domain["id"]
    doc = await _upload(alice, d, "a.txt")

    assert (await alice.request("DELETE", f"/documents/{doc['id']}")).status_code == 204

    assert (await alice.get(f"/domains/{d}/documents")).json()["total"] == 0
    assert (await alice.get(f"/domains/{d}/documents?include_trash=true")).json()["total"] == 1
    trash = (await alice.get(f"/domains/{d}/trash")).json()
    assert [x["id"] for x in trash["items"]] == [doc["id"]]
    assert trash["items"][0]["deleted_at"] is not None

    restored = await alice.post(f"/documents/{doc['id']}/restore")
    assert restored.status_code == 200
    assert restored.json()["deleted_at"] is None
    assert (await alice.get(f"/domains/{d}/documents")).json()["total"] == 1
    assert (await alice.get(f"/domains/{d}/trash")).json()["total"] == 0


async def test_delete_needs_delete_capability(alice, bob, domain):
    d = domain["id"]
    bob_me = (await bob.get("/auth/me")).json()
    doc = await _upload(alice, d, "b.txt")

    await alice.post(
        f"/domains/{d}/members", json={"username": bob_me["username"], "role": "editor"}
    )
    assert (await bob.request("DELETE", f"/documents/{doc['id']}")).status_code == 403

    await alice.patch(f"/domains/{d}/members/{bob_me['id']}", json={"role": "admin"})
    assert (await bob.request("DELETE", f"/documents/{doc['id']}")).status_code == 204


async def test_force_purge_is_owner_only(alice, bob, domain):
    d = domain["id"]
    bob_me = (await bob.get("/auth/me")).json()
    await alice.post(
        f"/domains/{d}/members", json={"username": bob_me["username"], "role": "admin"}
    )
    doc = await _upload(alice, d, "c.txt")
    await alice.request("DELETE", f"/documents/{doc['id']}")

    assert (await bob.post(f"/domains/{d}/trash/purge")).status_code == 403

    r = await alice.post(f"/domains/{d}/trash/purge")
    assert r.status_code == 200 and r.json()["purged"] == 1
    assert (await alice.get(f"/documents/{doc['id']}")).status_code == 404
    assert not storage.blob_exists(doc["sha256"])


async def test_restore_conflict_when_content_now_active(alice, domain):
    d = domain["id"]
    doc = await _upload(alice, d, "dup.txt", b"same bytes")
    await alice.request("DELETE", f"/documents/{doc['id']}")
    # a fresh upload of the same bytes under a new name creates a new active doc
    await _upload(alice, d, "dup2.txt", b"same bytes")

    r = await alice.post(f"/documents/{doc['id']}/restore")
    assert r.status_code == 409


async def test_cleanup_purges_expired_trash_and_gcs_blob(alice, domain, monkeypatch):
    from app.config import settings

    d = domain["id"]
    gone = await _upload(alice, d, "gone.txt", b"delete me")
    kept = await _upload(alice, d, "kept.txt", b"keep me")
    await alice.request("DELETE", f"/documents/{gone['id']}")

    monkeypatch.setattr(settings, "default_trash_retention_days", -1)
    stats = await run_cleanup()

    assert stats["purged_documents"] == 1
    assert (await alice.get(f"/documents/{gone['id']}")).status_code == 404
    assert not storage.blob_exists(gone["sha256"])
    assert (await alice.get(f"/documents/{kept['id']}/content")).status_code == 200
    assert storage.blob_exists(kept["sha256"])


async def test_cleanup_sweeps_orphan_blobs(alice, domain):
    d = domain["id"]
    doc = await _upload(alice, d, "real.txt", b"referenced")
    orphan = storage.store_bytes(b"nothing points at me")
    assert storage.blob_exists(orphan.sha256)

    stats = await run_cleanup()

    assert stats["orphan_blobs"] >= 1
    assert not storage.blob_exists(orphan.sha256)
    assert (await alice.get(f"/documents/{doc['id']}/content")).status_code == 200


async def test_cleanup_clears_expired_set_archive_but_keeps_row_and_link(alice, domain):
    d = domain["id"]
    doc = await _upload(alice, d, "s.txt")
    s = (
        await alice.post(
            f"/domains/{d}/sets", json={"name": "set", "document_ids": [doc["id"]]}
        )
    ).json()
    link = (
        await alice.post(f"/domains/{d}/sets/{s['id']}/links", json={"kind": "permanent"})
    ).json()
    assert (await _download_when_ready(alice, link["url"])).status_code == 200
    assert storage.set_archive_path(s["id"]).is_file()

    async with get_sessionmaker()() as db:
        art = await db.scalar(
            select(Artifact).where(Artifact.source_id == uuid.UUID(s["id"]))
        )
        art.expires_at = utcnow() - timedelta(days=1)
        await db.commit()

    stats = await run_cleanup()
    assert stats["cleared_set_archives"] == 1
    assert not storage.set_archive_path(s["id"]).is_file()

    # the link survives and the next hit rebuilds transparently
    assert (await _download_when_ready(alice, link["url"])).status_code == 200
