"""§7 rev 2 — the global tag pool: create-on-use, rename, merge, orphan sweep."""

import uuid

import pytest_asyncio

from app.db import get_sessionmaker
from app.models import User
from app.services import tags as tags_svc
from app.services.cleanup import run_cleanup


@pytest_asyncio.fixture
async def domain(alice):
    return (await alice.post("/api/domains", json={"name": "T"})).json()


async def _upload(client, d, name):
    return (
        await client.post(
            f"/api/domains/{d}/uploads", files={"file": (name, name.encode(), "text/plain")}
        )
    ).json()["document"]


async def test_tags_are_created_on_use_and_shared(alice, domain):
    d = domain["id"]
    a = await _upload(alice, d, "a.txt")
    await alice.patch(f"/api/documents/{a['id']}/tags", json={"tag_names": ["Договоры", "keep"]})

    other = (await alice.post("/api/domains", json={"name": "T2"})).json()["id"]
    b = await _upload(alice, other, "b.txt")
    await alice.patch(f"/api/documents/{b['id']}/tags", json={"tag_names": ["договоры"]})

    tags = {t["name"].lower(): t for t in (await alice.get("/api/tags/all")).json()}
    assert set(tags) == {"договоры", "keep"}
    assert tags["договоры"]["usage_count"] == 2


async def test_rename_and_merge(alice, domain):
    d = domain["id"]
    doc = await _upload(alice, d, "x.txt")
    await alice.patch(f"/api/documents/{doc['id']}/tags", json={"tag_names": ["old", "keep"]})
    tags = {t["name"]: t["id"] for t in (await alice.get("/api/tags/all")).json()}

    ren = await alice.patch(f"/api/tags/{tags['keep']}", json={"name": "Итоговый"})
    assert ren.json()["name"] == "Итоговый"

    r = await alice.post(f"/api/tags/{tags['old']}/merge", json={"into": tags["keep"]})
    assert r.status_code == 200
    doc_now = await alice.get(f"/api/documents/{doc['id']}")
    assert [t["name"] for t in doc_now.json()["tags"]] == ["Итоговый"]
    assert {t["name"] for t in (await alice.get("/api/tags/all")).json()} == {"Итоговый"}


async def test_orphan_sweep_removes_unused_tags(alice, domain):
    d = domain["id"]
    doc = await _upload(alice, d, "y.txt")
    await alice.patch(f"/api/documents/{doc['id']}/tags", json={"tag_names": ["temp"]})
    await alice.patch(f"/api/documents/{doc['id']}/tags", json={"tag_names": []})

    assert {t["name"] for t in (await alice.get("/api/tags/all")).json()} == {"temp"}
    stats = await run_cleanup()
    assert stats["orphan_tags"] == 1
    assert (await alice.get("/api/tags/all")).json() == []


async def test_bulk_add_tags_is_additive(alice, domain):
    d = domain["id"]
    d1 = await _upload(alice, d, "1.txt")
    d2 = await _upload(alice, d, "2.txt")
    await alice.patch(f"/api/documents/{d1['id']}/tags", json={"tag_names": ["shared"]})
    me = (await alice.get("/api/auth/me")).json()

    async with get_sessionmaker()() as db:
        owner = await db.get(User, uuid.UUID(me["id"]))
        tag_ids = await tags_svc.resolve_names(db, ["shared", "new"], actor=owner)
        n = await tags_svc.add_tags_to_documents(
            db, [uuid.UUID(d1["id"]), uuid.UUID(d2["id"])], tag_ids, actor=owner
        )
        await db.commit()
    assert n == 3  # d1 already had "shared"; the other three links are new
