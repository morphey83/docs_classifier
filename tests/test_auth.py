import pytest

REG = {"username": "Alice", "email": "alice@example.com", "password": "correct horse"}


async def test_register_logs_in_and_me(client):
    r = await client.post("/auth/register", json=REG)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["username"] == "alice"  # lower-cased
    assert body["email"] == "alice@example.com"
    assert client.cookies.get("dcsid")

    me = await client.get("/auth/me")
    assert me.status_code == 200
    assert me.json()["username"] == "alice"


async def test_register_duplicate_conflicts(client):
    await client.post("/auth/register", json=REG)
    r = await client.post(
        "/auth/register",
        json={"username": "alice", "email": "other@example.com", "password": "another one"},
    )
    assert r.status_code == 409


@pytest.mark.parametrize("bad", [{"password": "short"}, {"username": "a b"}, {"email": "nope"}])
async def test_register_validation(client, bad):
    r = await client.post("/auth/register", json={**REG, **bad})
    assert r.status_code == 422


async def test_login_wrong_password(client):
    await client.post("/auth/register", json=REG)
    await client.post("/auth/logout")
    r = await client.post("/auth/login", json={"login": "alice", "password": "nope"})
    assert r.status_code == 401


async def test_login_by_email_and_username(client):
    await client.post("/auth/register", json=REG)
    await client.post("/auth/logout")

    pw = REG["password"]
    r1 = await client.post("/auth/login", json={"login": "alice@example.com", "password": pw})
    assert r1.status_code == 200
    r2 = await client.post("/auth/login", json={"login": "ALICE", "password": pw})
    assert r2.status_code == 200


async def test_logout_invalidates_session(client):
    await client.post("/auth/register", json=REG)
    assert (await client.get("/auth/me")).status_code == 200
    await client.post("/auth/logout")
    assert (await client.get("/auth/me")).status_code == 401


async def test_me_requires_auth(client):
    assert (await client.get("/auth/me")).status_code == 401
