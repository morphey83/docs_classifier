from tests.conftest import register


async def test_create_domain_makes_owner(alice):
    r = await alice.post("/domains", json={"name": "Личные документы"})
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["my_role"] == "owner"
    assert d["slug"]  # cyrillic slug still produced

    lst = await alice.get("/domains")
    assert [x["id"] for x in lst.json()] == [d["id"]]


async def test_non_member_gets_404(alice, bob):
    d = (await alice.post("/domains", json={"name": "Secret"})).json()
    r = await bob.get(f"/domains/{d['id']}")
    assert r.status_code == 404


async def test_add_member_and_role_capabilities(alice, bob):
    d = (await alice.post("/domains", json={"name": "Shared"})).json()

    add = await alice.post(
        f"/domains/{d['id']}/members", json={"username": "bob", "role": "viewer"}
    )
    assert add.status_code == 201

    # viewer can read but not upload / not manage
    assert (await bob.get(f"/domains/{d['id']}")).status_code == 200
    up = await bob.post(
        f"/domains/{d['id']}/uploads", files={"file": ("x.txt", b"hi", "text/plain")}
    )
    assert up.status_code == 403
    assert (
        await bob.post(f"/domains/{d['id']}/members", json={"username": "alice", "role": "admin"})
    ).status_code == 403

    # promote to editor -> can upload
    await alice.patch(
        f"/domains/{d['id']}/members/{(await bob.get('/auth/me')).json()['id']}",
        json={"role": "editor"},
    )
    up2 = await bob.post(
        f"/domains/{d['id']}/uploads", files={"file": ("x.txt", b"hi", "text/plain")}
    )
    assert up2.status_code == 201


async def test_cannot_assign_owner_role(alice, bob):
    d = (await alice.post("/domains", json={"name": "D"})).json()
    r = await alice.post(f"/domains/{d['id']}/members", json={"username": "bob", "role": "owner"})
    assert r.status_code == 422


async def test_invite_flow(alice, client_factory):
    carol = client_factory()
    await register(carol, "carol")

    d = (await alice.post("/domains", json={"name": "Invited"})).json()
    inv = await alice.post(
        f"/domains/{d['id']}/invites", json={"role": "editor", "username": "carol"}
    )
    assert inv.status_code == 201
    token = inv.json()["token"]

    acc = await carol.post(f"/invites/{token}/accept")
    assert acc.status_code == 200
    assert acc.json()["my_role"] == "editor"

    # second accept fails
    assert (await carol.post(f"/invites/{token}/accept")).status_code == 400


async def test_owner_cannot_be_removed(alice):
    d = (await alice.post("/domains", json={"name": "D"})).json()
    me = (await alice.get("/auth/me")).json()["id"]
    r = await alice.delete(f"/domains/{d['id']}/members/{me}")
    assert r.status_code == 400


async def test_delete_domain_requires_owner(alice, bob):
    d = (await alice.post("/domains", json={"name": "D"})).json()
    await alice.post(f"/domains/{d['id']}/members", json={"username": "bob", "role": "admin"})
    assert (await bob.delete(f"/domains/{d['id']}")).status_code == 403
    assert (await alice.delete(f"/domains/{d['id']}")).status_code == 204
