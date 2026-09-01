"""Phase 4 — document sets, the rebuild-on-demand archive cache, share links."""

import io
import zipfile

import pytest_asyncio

from app.util import ratelimit


@pytest_asyncio.fixture(autouse=True)
def _reset_ratelimit():
    ratelimit.reset()
    yield
    ratelimit.reset()


@pytest_asyncio.fixture
async def domain(alice):
    return (await alice.post("/domains", json={"name": "P4"})).json()


async def _upload(client, domain_id, name, body=None):
    body = body if body is not None else name.encode()
    r = await client.post(
        f"/domains/{domain_id}/uploads", files={"file": (name, body, "text/plain")}
    )
    assert r.status_code in (201, 202), r.text
    return r.json()["document"]


async def _download_when_ready(client, url, tries=3):
    for _ in range(tries):
        r = await client.get(url)
        if r.status_code == 200:
            return r
    return r


async def test_set_crud_and_items(alice, domain):
    d = domain["id"]
    docs = [await _upload(alice, d, f"f{i}.txt") for i in range(3)]

    s = (await alice.post(f"/domains/{d}/sets", json={"name": "Q3 docs"})).json()
    assert s["item_count"] == 0 and s["visibility"] == "private"

    detail = (
        await alice.post(
            f"/domains/{d}/sets/{s['id']}/items",
            json={"document_ids": [docs[0]["id"], docs[1]["id"], docs[0]["id"]]},
        )
    ).json()
    assert detail["item_count"] == 2  # duplicate ignored
    assert [i["document"]["id"] for i in detail["items"]] == [docs[0]["id"], docs[1]["id"]]

    detail = (
        await alice.request(
            "DELETE", f"/domains/{d}/sets/{s['id']}/items/{docs[0]['id']}"
        )
    ).json()
    assert detail["item_count"] == 1

    listed = (await alice.get(f"/domains/{d}/sets")).json()
    assert [x["id"] for x in listed] == [s["id"]]


async def test_archive_builds_lazily_and_rebuilds_on_change(alice, domain):
    d = domain["id"]
    docs = [await _upload(alice, d, f"g{i}.txt") for i in range(2)]
    s = (
        await alice.post(
            f"/domains/{d}/sets",
            json={"name": "pack", "document_ids": [docs[0]["id"], docs[1]["id"]]},
        )
    ).json()

    first = await alice.get(f"/domains/{d}/sets/{s['id']}/archive/download")
    assert first.status_code == 202
    assert first.json()["status"] == "building"

    ready = await _download_when_ready(alice, f"/domains/{d}/sets/{s['id']}/archive/download")
    assert ready.status_code == 200
    with zipfile.ZipFile(io.BytesIO(ready.content)) as zf:
        assert sum(n.startswith("files/") for n in zf.namelist()) == 2

    status = (await alice.get(f"/domains/{d}/sets/{s['id']}/archive")).json()
    assert status["ready"] and status["item_count"] == 2

    # change the set -> next request transparently rebuilds
    await alice.request("DELETE", f"/domains/{d}/sets/{s['id']}/items/{docs[1]['id']}")
    after = await alice.get(f"/domains/{d}/sets/{s['id']}/archive/download")
    assert after.status_code == 202  # stale, rebuild queued

    ready2 = await _download_when_ready(alice, f"/domains/{d}/sets/{s['id']}/archive/download")
    assert ready2.status_code == 200
    with zipfile.ZipFile(io.BytesIO(ready2.content)) as zf:
        assert sum(n.startswith("files/") for n in zf.namelist()) == 1


async def test_set_visibility_between_members(alice, bob, domain):
    d = domain["id"]
    bob_me = (await bob.get("/auth/me")).json()
    await alice.post(
        f"/domains/{d}/members", json={"username": bob_me["username"], "role": "editor"}
    )

    priv = (await alice.post(f"/domains/{d}/sets", json={"name": "mine"})).json()
    shared = (
        await alice.post(
            f"/domains/{d}/sets", json={"name": "ours", "visibility": "domain"}
        )
    ).json()

    assert (await bob.get(f"/domains/{d}/sets/{priv['id']}")).status_code == 404
    assert (await bob.get(f"/domains/{d}/sets/{shared['id']}")).status_code == 200
    assert {x["id"] for x in (await bob.get(f"/domains/{d}/sets")).json()} == {shared["id"]}

    # editor is not the creator and lacks 'manage' -> cannot edit the shared set
    assert (
        await bob.patch(f"/domains/{d}/sets/{shared['id']}", json={"name": "hijack"})
    ).status_code == 403


async def test_one_time_link_public_download(alice, domain):
    d = domain["id"]
    doc = await _upload(alice, d, "report.txt")
    s = (
        await alice.post(
            f"/domains/{d}/sets", json={"name": "rep", "document_ids": [doc["id"]]}
        )
    ).json()

    link = (
        await alice.post(f"/domains/{d}/sets/{s['id']}/links", json={"kind": "one_time"})
    ).json()
    assert link["max_downloads"] == 1 and link["url"] == f"/d/{link['token']}"

    got = await _download_when_ready(alice, link["url"])
    assert got.status_code == 200
    assert got.headers["content-type"] == "application/zip"

    # one-time -> spent
    assert (await alice.get(link["url"])).status_code == 410


async def test_permanent_link_needs_write_and_stays_live(alice, bob, domain):
    d = domain["id"]
    bob_me = (await bob.get("/auth/me")).json()
    await alice.post(
        f"/domains/{d}/members", json={"username": bob_me["username"], "role": "viewer"}
    )
    doc = await _upload(alice, d, "p.txt")
    s = (
        await alice.post(
            f"/domains/{d}/sets",
            json={"name": "s", "visibility": "domain", "document_ids": [doc["id"]]},
        )
    ).json()

    # viewer has download but not write -> no permanent link
    assert (
        await bob.post(f"/domains/{d}/sets/{s['id']}/links", json={"kind": "permanent"})
    ).status_code == 403
    assert (
        await bob.post(f"/domains/{d}/sets/{s['id']}/links", json={"kind": "one_time"})
    ).status_code == 201

    link = (
        await alice.post(
            f"/domains/{d}/sets/{s['id']}/links", json={"kind": "permanent"}
        )
    ).json()
    assert link["max_downloads"] is None

    assert (await _download_when_ready(alice, link["url"])).status_code == 200
    assert (await _download_when_ready(alice, link["url"])).status_code == 200  # reusable

    # revoke -> dead
    assert (await alice.request("DELETE", f"/links/{link['id']}")).status_code == 204
    assert (await alice.get(link["url"])).status_code == 404


async def test_public_link_dies_when_owner_loses_download(alice, bob, domain):
    d = domain["id"]
    bob_me = (await bob.get("/auth/me")).json()
    await alice.post(
        f"/domains/{d}/members", json={"username": bob_me["username"], "role": "editor"}
    )
    doc = await _upload(alice, d, "x.txt")
    s = (
        await bob.post(
            f"/domains/{d}/sets",
            json={"name": "b", "visibility": "domain", "document_ids": [doc["id"]]},
        )
    ).json()
    link = (
        await bob.post(f"/domains/{d}/sets/{s['id']}/links", json={"kind": "permanent"})
    ).json()
    assert (await _download_when_ready(bob, link["url"])).status_code == 200

    # scanner: view + process, no download
    await alice.patch(
        f"/domains/{d}/members/{bob_me['id']}", json={"role": "scanner"}
    )
    assert (await alice.get(link["url"])).status_code == 403


async def test_allow_public_links_toggle(alice, domain):
    d = domain["id"]
    doc = await _upload(alice, d, "n.txt")
    s = (
        await alice.post(
            f"/domains/{d}/sets", json={"name": "n", "document_ids": [doc["id"]]}
        )
    ).json()
    await alice.patch(f"/domains/{d}", json={"settings": {"allow_public_links": False}})

    assert (
        await alice.post(f"/domains/{d}/sets/{s['id']}/links", json={"kind": "one_time"})
    ).status_code == 403


async def test_set_and_archive_hidden_from_non_member(alice, bob, domain):
    d = domain["id"]
    doc = await _upload(alice, d, "z.txt")
    s = (
        await alice.post(
            f"/domains/{d}/sets", json={"name": "z", "document_ids": [doc["id"]]}
        )
    ).json()
    assert (await bob.get(f"/domains/{d}/sets/{s['id']}")).status_code == 404
    assert (
        await bob.get(f"/domains/{d}/sets/{s['id']}/archive/download")
    ).status_code == 404
