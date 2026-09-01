import pytest_asyncio


@pytest_asyncio.fixture
async def domain(alice):
    return (await alice.post("/api/domains", json={"name": "T"})).json()


async def test_tag_crud(alice, domain):
    d = domain["id"]
    made = await alice.post(f"/api/domains/{d}/tags", json={"name": "Договоры", "color": "#f00"})
    assert made.status_code == 201
    tid = made.json()["id"]

    dup = await alice.post(f"/api/domains/{d}/tags", json={"name": "договоры"})
    assert dup.status_code == 409  # same slug

    ren = await alice.patch(f"/api/domains/{d}/tags/{tid}", json={"name": "Соглашения"})
    assert ren.json()["name"] == "Соглашения"

    assert (await alice.delete(f"/api/domains/{d}/tags/{tid}")).status_code == 204
    assert (await alice.get(f"/api/domains/{d}/tags")).json() == []


async def test_tag_merge_moves_documents(alice, domain):
    d = domain["id"]
    doc = (
        await alice.post(f"/api/domains/{d}/uploads", files={"file": ("a.txt", b"a", "text/plain")})
    ).json()["document"]
    await alice.patch(f"/api/documents/{doc['id']}/tags", json={"tag_names": ["old", "keep"]})

    tags = {t["name"]: t["id"] for t in (await alice.get(f"/api/domains/{d}/tags")).json()}
    r = await alice.post(f"/api/domains/{d}/tags/{tags['old']}/merge", json={"into": tags["keep"]})
    assert r.status_code == 200

    remaining = {t["name"] for t in (await alice.get(f"/api/domains/{d}/tags")).json()}
    assert remaining == {"keep"}
    doc_now = await alice.get(f"/api/documents/{doc['id']}")
    assert [t["name"] for t in doc_now.json()["tags"]] == ["keep"]


async def test_tagger_can_create_but_not_delete(alice, bob, domain):
    d = domain["id"]
    await alice.post(f"/api/domains/{d}/members", json={"username": "bob", "role": "tagger"})

    made = await bob.post(f"/api/domains/{d}/tags", json={"name": "bobtag"})
    assert made.status_code == 201
    assert (await bob.delete(f"/api/domains/{d}/tags/{made.json()['id']}")).status_code == 403
