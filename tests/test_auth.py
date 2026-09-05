import pytest

REG = {"username": "Alice", "email": "alice@example.com", "password": "correct horse"}


async def test_register_logs_in_and_me(client):
    r = await client.post("/api/auth/register", json=REG)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["username"] == "alice"  # lower-cased
    assert body["email"] == "alice@example.com"
    assert client.cookies.get("dcsid")

    me = await client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["username"] == "alice"


async def test_register_duplicate_conflicts(client):
    await client.post("/api/auth/register", json=REG)
    r = await client.post(
        "/api/auth/register",
        json={"username": "alice", "email": "other@example.com", "password": "another one"},
    )
    assert r.status_code == 409


@pytest.mark.parametrize("bad", [{"password": "short"}, {"username": "a b"}, {"email": "nope"}])
async def test_register_validation(client, bad):
    r = await client.post("/api/auth/register", json={**REG, **bad})
    assert r.status_code == 422


async def test_login_wrong_password(client):
    await client.post("/api/auth/register", json=REG)
    await client.post("/api/auth/logout")
    r = await client.post("/api/auth/login", json={"login": "alice", "password": "nope"})
    assert r.status_code == 401


async def test_login_by_email_and_username(client):
    await client.post("/api/auth/register", json=REG)
    await client.post("/api/auth/logout")

    pw = REG["password"]
    r1 = await client.post("/api/auth/login", json={"login": "alice@example.com", "password": pw})
    assert r1.status_code == 200
    r2 = await client.post("/api/auth/login", json={"login": "ALICE", "password": pw})
    assert r2.status_code == 200


async def test_logout_invalidates_session(client):
    await client.post("/api/auth/register", json=REG)
    assert (await client.get("/api/auth/me")).status_code == 200
    await client.post("/api/auth/logout")
    assert (await client.get("/api/auth/me")).status_code == 401


async def test_me_requires_auth(client):
    assert (await client.get("/api/auth/me")).status_code == 401


# --- per-device API keys (Bearer auth for native/mobile clients) ---------
async def test_api_key_bearer_auth_works_without_cookie(alice, client_factory):
    created = await alice.post("/api/auth/api-keys", json={"name": "Pixel"})
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["name"] == "Pixel"
    token = body["token"]
    assert token and token.startswith("dc_")

    bare = client_factory()  # no session cookie at all
    me = await bare.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200 and me.json()["username"] == "alice"


async def test_api_key_list_hides_token_and_omits_revoked(alice):
    r1 = await alice.post("/api/auth/api-keys", json={"name": "A"})
    await alice.post("/api/auth/api-keys", json={"name": "B"})
    key_id = r1.json()["id"]

    listed = (await alice.get("/api/auth/api-keys")).json()
    assert {k["name"] for k in listed} == {"A", "B"}
    assert all(k["token"] is None for k in listed)

    revoke = await alice.request("DELETE", f"/api/auth/api-keys/{key_id}")
    assert revoke.status_code == 204
    listed2 = (await alice.get("/api/auth/api-keys")).json()
    assert {k["name"] for k in listed2} == {"B"}


async def test_revoked_api_key_stops_authenticating(alice, client_factory):
    created = (await alice.post("/api/auth/api-keys", json={"name": "Old phone"})).json()
    token = created["token"]
    await alice.request("DELETE", f"/api/auth/api-keys/{created['id']}")

    bare = client_factory()
    me = await bare.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 401


async def test_garbage_bearer_token_is_401(client_factory):
    bare = client_factory()
    me = await bare.get("/api/auth/me", headers={"Authorization": "Bearer nonsense"})
    assert me.status_code == 401


async def test_api_key_cannot_revoke_another_users_key(alice, bob):
    created = (await alice.post("/api/auth/api-keys", json={"name": "Mine"})).json()
    r = await bob.request("DELETE", f"/api/auth/api-keys/{created['id']}")
    assert r.status_code == 404
