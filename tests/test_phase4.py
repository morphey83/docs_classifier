"""§15 rev 4 — user-owned sets: saved filters + explicit adds, public archive, links."""

import io
import uuid
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
    return (await alice.post("/api/domains", json={"name": "P4"})).json()


async def _upload(client, domain_id, name, body=None, *, public=False):
    body = body if body is not None else name.encode()
    r = await client.post(
        f"/api/domains/{domain_id}/uploads", files={"file": (name, body, "text/plain")}
    )
    assert r.status_code in (201, 202), r.text
    doc = r.json()["document"]
    if public:
        await client.post(f"/api/documents/{doc['id']}/visibility?is_public=true")
    return doc


async def _ready(client, url, tries=4):
    r = None
    for _ in range(tries):
        r = await client.get(url)
        if r.status_code == 200:
            return r
    return r


def _names(content):
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        return sorted(n for n in zf.namelist() if n.startswith("files/"))


# --- CRUD ---------------------------------------------------------------
async def test_set_crud_and_explicit_items(alice, domain):
    d = domain["id"]
    docs = [await _upload(alice, d, f"f{i}.txt") for i in range(3)]

    s = (await alice.post("/api/sets", json={"name": "Q3"})).json()
    assert s["owner_id"] and "visibility" not in s

    detail = (
        await alice.post(
            f"/api/sets/{s['id']}/items",
            json={"document_ids": [docs[0]["id"], docs[1]["id"], docs[0]["id"]]},
        )
    ).json()
    assert detail["item_count"] == 2  # duplicate ignored

    detail = (
        await alice.request("DELETE", f"/api/sets/{s['id']}/items/{docs[0]['id']}")
    ).json()
    assert detail["item_count"] == 1

    assert [x["id"] for x in (await alice.get("/api/sets")).json()] == [s["id"]]


async def test_a_set_is_private_to_its_owner(alice, bob, domain):
    d = domain["id"]
    bob_me = (await bob.get("/api/auth/me")).json()
    await alice.post(
        f"/api/domains/{d}/members", json={"username": bob_me["username"], "role": "editor"}
    )
    s = (await alice.post("/api/sets", json={"name": "mine"})).json()

    assert (await bob.get(f"/api/sets/{s['id']}")).status_code == 404
    assert (await bob.get("/api/sets")).json() == []
    assert (await bob.patch(f"/api/sets/{s['id']}", json={"name": "hijack"})).status_code == 404
    assert (await bob.get(f"/api/sets/{s['id']}/archive/download")).status_code == 404


# --- dynamic filters --------------------------------------------------
async def test_saved_filter_pulls_in_matching_documents(alice, domain):
    d = domain["id"]
    a = await _upload(alice, d, "invoice-1.txt", public=True)
    await _upload(alice, d, "photo.txt", public=True)
    await alice.patch(f"/api/documents/{a['id']}", json={"title": "invoice one"})

    s = (await alice.post("/api/sets", json={"name": "invoices"})).json()
    detail = (
        await alice.post(
            f"/api/sets/{s['id']}/filters",
            json={"filter": {"q": "invoice"}, "description": "«invoice»"},
        )
    ).json()
    assert detail["resolved_count"] == 1

    # a new matching document shows up with no edit to the set
    b = await _upload(alice, d, "invoice-2.txt", public=True)
    await alice.patch(f"/api/documents/{b['id']}", json={"title": "invoice two"})
    detail = (await alice.get(f"/api/sets/{s['id']}")).json()
    assert detail["resolved_count"] == 2


# --- shareable archive (public docs only) ---------------------------
async def test_archive_contains_only_public_documents(alice, domain):
    d = domain["id"]
    pub = await _upload(alice, d, "pub.txt", public=True)
    priv = await _upload(alice, d, "priv.txt")
    s = (
        await alice.post(
            "/api/sets", json={"name": "mix", "document_ids": [pub["id"], priv["id"]]}
        )
    ).json()

    ready = await _ready(alice, f"/api/sets/{s['id']}/archive/download")
    assert ready.status_code == 200
    assert _names(ready.content) == ["files/pub.txt"]

    # flipping the private one public rebuilds with both
    await alice.post(f"/api/documents/{priv['id']}/visibility?is_public=true")
    assert (await alice.get(f"/api/sets/{s['id']}/archive/download")).status_code == 202
    ready2 = await _ready(alice, f"/api/sets/{s['id']}/archive/download")
    assert _names(ready2.content) == ["files/priv.txt", "files/pub.txt"]


async def test_a_deleted_document_never_enters_an_archive(alice, domain):
    d = domain["id"]
    keep = await _upload(alice, d, "keep.txt", public=True)
    gone = await _upload(alice, d, "gone.txt", public=True)
    s = (
        await alice.post(
            "/api/sets", json={"name": "x", "document_ids": [keep["id"], gone["id"]]}
        )
    ).json()

    # explicitly pinned, then trashed
    assert (await alice.request("DELETE", f"/api/documents/{gone['id']}")).status_code == 204

    ready = await _ready(alice, f"/api/sets/{s['id']}/archive/download")
    assert _names(ready.content) == ["files/keep.txt"]

    full = (await alice.post(f"/api/sets/{s['id']}/export")).json()
    got = await _ready(alice, f"/api/artifacts/{full['id']}/download")
    assert _names(got.content) == ["files/keep.txt"]


async def test_full_export_includes_private_documents(alice, domain):
    d = domain["id"]
    pub = await _upload(alice, d, "a.txt", public=True)
    priv = await _upload(alice, d, "b.txt")
    s = (
        await alice.post(
            "/api/sets", json={"name": "all", "document_ids": [pub["id"], priv["id"]]}
        )
    ).json()

    art = (await alice.post(f"/api/sets/{s['id']}/export")).json()
    assert art["domain_id"] is None and art["kind"] == "adhoc_export"
    got = await _ready(alice, f"/api/artifacts/{art['id']}/download")
    assert got.status_code == 200
    assert _names(got.content) == ["files/a.txt", "files/b.txt"]


# --- share links -------------------------------------------------
async def test_one_time_and_permanent_links(alice, domain):
    d = domain["id"]
    doc = await _upload(alice, d, "report.txt", public=True)
    s = (
        await alice.post("/api/sets", json={"name": "rep", "document_ids": [doc["id"]]})
    ).json()

    once = (await alice.post(f"/api/sets/{s['id']}/links", json={"kind": "one_time"})).json()
    assert once["max_downloads"] == 1
    assert (await _ready(alice, once["url"])).status_code == 200
    assert (await alice.get(once["url"])).status_code == 410  # spent

    perm = (await alice.post(f"/api/sets/{s['id']}/links", json={"kind": "permanent"})).json()
    assert perm["max_downloads"] is None
    assert (await _ready(alice, perm["url"])).status_code == 200
    assert (await _ready(alice, perm["url"])).status_code == 200  # reusable

    assert (await alice.request("DELETE", f"/api/links/{perm['id']}")).status_code == 204
    assert (await alice.get(perm["url"])).status_code == 404


async def test_share_link_limits_per_kind(alice, domain):
    d = domain["id"]
    doc = await _upload(alice, d, "lim.txt", public=True)
    s = (
        await alice.post("/api/sets", json={"name": "lim", "document_ids": [doc["id"]]})
    ).json()

    links = f"/api/sets/{s['id']}/links"
    assert (await alice.post(links, json={"kind": "permanent"})).status_code == 201
    # a second permanent link is refused — only one per set
    dup = await alice.post(links, json={"kind": "permanent"})
    assert dup.status_code == 409 and "лимит" in dup.json()["detail"]
    # a one-time link is a different kind and still allowed
    assert (await alice.post(links, json={"kind": "one_time"})).status_code == 201
    # …and once that one-time link is spent it stops counting → room for another
    once = (await alice.get(links)).json()
    tok = next(link_["url"] for link_ in once if link_["max_downloads"] == 1)
    await _ready(alice, tok)  # download it → spent
    assert (await alice.post(links, json={"kind": "one_time"})).status_code == 201


async def test_gallery_link_serves_only_public_images(alice, domain):
    d = domain["id"]
    img = await _upload(alice, d, "pic.png", b"\x89PNG\r\n\x1a\n" + b"0" * 40, public=True)
    txt = await _upload(alice, d, "note.txt", public=True)
    priv = await _upload(alice, d, "secret.png", b"\x89PNG\r\n\x1a\n" + b"1" * 40)
    s = (
        await alice.post(
            "/api/sets",
            json={"name": "gal", "document_ids": [img["id"], txt["id"], priv["id"]]},
        )
    ).json()

    from app.db import get_sessionmaker
    from app.models import DocumentSet, User
    from app.services import docsets as dsvc

    async with get_sessionmaker()() as db_:
        me = (await alice.get("/api/auth/me")).json()
        owner = await db_.get(User, uuid.UUID(me["id"]))
        set_obj = await db_.get(DocumentSet, uuid.UUID(s["id"]))
        link = await dsvc.create_share_link(db_, None, s=set_obj, user=owner, mode="gallery")
        token = link.token
        await db_.commit()

    j = (await alice.get(f"/g/{token}.json")).json()
    ids = {doc["id"] for doc in j["documents"]}
    assert img["id"] in ids and txt["id"] in ids and priv["id"] not in ids

    page = await alice.get(f"/g/{token}")
    assert page.status_code == 200 and "gal" in page.text

    # per-image serve: allowed for a set member, refused for the private one
    assert (await alice.get(f"/g/{token}/i/{img['id']}")).status_code == 200
    assert (await alice.get(f"/g/{token}/i/{priv['id']}")).status_code == 404

    feed = await alice.get(f"/g/{token}/feed")
    assert feed.status_code == 200 and "atom" in feed.headers["content-type"]

    # the archive link route must not accept a gallery token
    assert (await alice.get(f"/d/{token}")).status_code == 404


async def test_public_link_serves_nothing_once_docs_go_private(alice, domain):
    d = domain["id"]
    doc = await _upload(alice, d, "x.txt", public=True)
    s = (
        await alice.post("/api/sets", json={"name": "x", "document_ids": [doc["id"]]})
    ).json()
    link = (await alice.post(f"/api/sets/{s['id']}/links", json={"kind": "permanent"})).json()
    assert (await _ready(alice, link["url"])).status_code == 200

    await alice.post(f"/api/documents/{doc['id']}/visibility?is_public=false")
    # rebuild, then the (now empty) archive is gone
    for _ in range(4):
        r = await alice.get(link["url"])
        if r.status_code != 202:
            break
    assert r.status_code == 410


async def test_public_link_dies_when_owner_loses_download(alice, bob, domain):
    d = domain["id"]
    bob_me = (await bob.get("/api/auth/me")).json()
    await alice.post(
        f"/api/domains/{d}/members", json={"username": bob_me["username"], "role": "editor"}
    )
    doc = await _upload(alice, d, "z.txt", public=True)
    s = (await bob.post("/api/sets", json={"name": "b", "document_ids": [doc["id"]]})).json()
    link = (await bob.post(f"/api/sets/{s['id']}/links", json={"kind": "permanent"})).json()
    assert (await _ready(bob, link["url"])).status_code == 200

    await alice.patch(f"/api/domains/{d}/members/{bob_me['id']}", json={"role": "scanner"})
    for _ in range(4):
        r = await alice.get(link["url"])
        if r.status_code != 202:
            break
    assert r.status_code == 410
