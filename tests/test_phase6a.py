"""Phase 6a — cross-domain search, allowed file types, linking, absolute links."""

import io
import uuid
import zipfile

import pytest_asyncio

from app.db import get_sessionmaker
from app.models import User
from app.services import tglink as tglink_svc


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


async def _upload(client, domain_id, name, body=None, ct="text/plain"):
    body = body if body is not None else name.encode()
    return await client.post(
        f"/api/domains/{domain_id}/uploads", files={"file": (name, body, ct)}
    )


# --- cross-domain search --------------------------------------------------
async def test_cross_domain_search_spans_all_memberships(alice):
    a = (await alice.post("/api/domains", json={"name": "A"})).json()
    b = (await alice.post("/api/domains", json={"name": "B"})).json()
    da = (await _upload(alice, a["id"], "in-a.txt")).json()["document"]
    db_ = (await _upload(alice, b["id"], "in-b.txt")).json()["document"]

    all_docs = (await alice.get("/api/documents")).json()
    assert all_docs["total"] == 2
    names = {(x["title"], x["domain_name"]) for x in all_docs["items"]}
    assert names == {("in-a", "A"), ("in-b", "B")}

    only_a = (await alice.get(f"/api/documents?domain_id={a['id']}")).json()
    assert only_a["total"] == 1
    assert only_a["items"][0]["id"] == da["id"]
    assert db_["id"] not in [x["id"] for x in only_a["items"]]


async def test_cross_domain_search_rejects_foreign_domain(alice, bob):
    a = (await alice.post("/api/domains", json={"name": "A2"})).json()
    assert (await bob.get(f"/api/documents?domain_id={a['id']}")).status_code == 404


async def test_cross_domain_tag_filter_matches_by_name_case_insensitive(alice):
    a = (await alice.post("/api/domains", json={"name": "TA"})).json()
    b = (await alice.post("/api/domains", json={"name": "TB"})).json()
    d1 = (await _upload(alice, a["id"], "one.txt")).json()["document"]
    d2 = (await _upload(alice, b["id"], "two.txt")).json()["document"]
    await alice.patch(f"/api/documents/{d1['id']}/tags", json={"tag_names": ["Договор"]})
    await alice.patch(f"/api/documents/{d2['id']}/tags", json={"tag_names": ["договор"]})

    hit = (await alice.get("/api/documents?tags=ДОГОВОР")).json()
    assert {x["id"] for x in hit["items"]} == {d1["id"], d2["id"]}


async def test_cross_domain_tag_options_aggregate_usage(alice):
    a = (await alice.post("/api/domains", json={"name": "TC"})).json()
    b = (await alice.post("/api/domains", json={"name": "TD"})).json()
    d1 = (await _upload(alice, a["id"], "x1.txt")).json()["document"]
    d2 = (await _upload(alice, a["id"], "x2.txt")).json()["document"]
    d3 = (await _upload(alice, b["id"], "x3.txt")).json()["document"]
    for d in (d1, d2):
        await alice.patch(f"/api/documents/{d['id']}/tags", json={"tag_names": ["invoice"]})
    await alice.patch(f"/api/documents/{d3['id']}/tags", json={"tag_names": ["Invoice"]})

    options = (await alice.get("/api/tags")).json()
    assert {o["name"].lower(): o["usage_count"] for o in options} == {"invoice": 3}


# --- allowed file types ----------------------------------------------------
async def test_disallowed_direct_upload_is_rejected(alice):
    d = (await alice.post("/api/domains", json={"name": "AT1"})).json()
    await alice.patch(f"/api/domains/{d['id']}", json={"settings": {"allowed_types": ["txt"]}})

    r = await _upload(alice, d["id"], "doc.pdf", b"whatever", ct="application/pdf")
    assert r.status_code == 415
    assert r.json()["detail"]["error"] == "type_not_allowed"
    assert r.json()["detail"]["allowed"] == ["txt"]

    ok = await _upload(alice, d["id"], "doc.txt")
    assert ok.status_code == 201


async def test_disallowed_archive_entries_are_skipped_not_fatal(alice):
    d = (await alice.post("/api/domains", json={"name": "AT2"})).json()
    await alice.patch(f"/api/domains/{d['id']}", json={"settings": {"allowed_types": ["txt"]}})

    archive = _zip({"good.txt": b"ok", "bad.pdf": b"nope", "also.png": b"nope2"})
    r = await alice.post(
        f"/api/domains/{d['id']}/uploads", files={"file": ("pack.zip", archive, "application/zip")}
    )
    batch_id = r.json()["id"]
    detail = (await alice.get(f"/api/domains/{d['id']}/uploads/{batch_id}")).json()

    assert detail["status"] == "done"
    assert detail["item_count"] == 1
    outcomes = {i["entry_name"]: i["outcome"] for i in detail["items"]}
    assert outcomes["good.txt"] == "created"
    assert outcomes["bad.pdf"] == "skipped_type"
    assert outcomes["also.png"] == "skipped_type"
    bad_item = next(i for i in detail["items"] if i["entry_name"] == "bad.pdf")
    assert "not allowed" in bad_item["note"]


# --- auto-reindex on title edit --------------------------------------------
async def test_title_edit_refreshes_search_when_already_indexed(alice):
    d = (await alice.post("/api/domains", json={"name": "RI"})).json()
    doc = (await _upload(alice, d["id"], "memo.txt", "старое содержимое".encode())).json()[
        "document"
    ]
    await alice.post(f"/api/documents/{doc['id']}/index")

    await alice.patch(f"/api/documents/{doc['id']}", json={"title": "уникальныйзаголовок"})

    hit = await alice.get(f"/api/domains/{d['id']}/documents?q=уникальныйзаголовок")
    assert [x["id"] for x in hit.json()["items"]] == [doc["id"]]


# --- PUBLIC_BASE_URL ---------------------------------------------------
async def test_share_link_is_absolute_when_public_base_url_set(alice, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "public_base_url", "https://docs.example.com")
    d = (await alice.post("/api/domains", json={"name": "PB"})).json()
    doc = (await _upload(alice, d["id"], "p.txt")).json()["document"]
    s = (
        await alice.post(
            f"/api/domains/{d['id']}/sets", json={"name": "s", "document_ids": [doc["id"]]}
        )
    ).json()
    link = (
        await alice.post(f"/api/domains/{d['id']}/sets/{s['id']}/links", json={"kind": "one_time"})
    ).json()
    assert link["url"] == f"https://docs.example.com/d/{link['token']}"


# --- Telegram linking -------------------------------------------------
@pytest_asyncio.fixture
async def bot_username(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "telegram_bot_username", "DocsClassifierBot")


async def test_web_initiated_link_returns_deep_link(alice, bot_username):
    r = await alice.post("/api/auth/tg-link")
    assert r.status_code == 201
    body = r.json()
    assert body["deep_link"] == f"https://t.me/DocsClassifierBot?start={body['token']}"


async def test_web_initiated_link_deep_link_none_without_bot_username(alice):
    r = await alice.post("/api/auth/tg-link")
    assert r.json()["deep_link"] is None


async def test_bot_initiated_flow_confirmed_from_web(alice):
    async with get_sessionmaker()() as db:
        tok = await tglink_svc.create_bot_initiated(db, tg_id=555, tg_username="ivan")
        await db.commit()
        token = tok.token

    status_before = (await alice.get(f"/tg/link/{token}/status")).json()
    assert status_before == {"valid": True, "kind": "bot", "tg_username": "ivan", "reason": None}

    page = await alice.get(f"/tg/link/{token}")
    assert page.status_code == 200 and "Привязка Telegram" in page.text

    confirm = await alice.post(f"/tg/link/{token}/confirm")
    assert confirm.status_code == 200 and confirm.json() == {"linked": True}

    me = (await alice.get("/api/auth/me")).json()
    assert me["tg_id"] == 555

    status_after = (await alice.get(f"/tg/link/{token}/status")).json()
    assert status_after["valid"] is False


async def test_confirm_rejects_when_tg_id_already_linked_elsewhere(alice, bob):
    async with get_sessionmaker()() as db:
        first = await tglink_svc.create_bot_initiated(db, tg_id=777, tg_username="taken")
        second = await tglink_svc.create_bot_initiated(db, tg_id=777, tg_username="taken")
        await db.commit()
        t1, t2 = first.token, second.token

    assert (await alice.post(f"/tg/link/{t1}/confirm")).status_code == 200
    r = await bob.post(f"/tg/link/{t2}/confirm")
    assert r.status_code == 409


async def test_confirm_rejects_when_account_already_linked(alice):
    async with get_sessionmaker()() as db:
        t1 = (await tglink_svc.create_bot_initiated(db, tg_id=1, tg_username="a")).token
        t2 = (await tglink_svc.create_bot_initiated(db, tg_id=2, tg_username="b")).token
        await db.commit()

    assert (await alice.post(f"/tg/link/{t1}/confirm")).status_code == 200
    assert (await alice.post(f"/tg/link/{t2}/confirm")).status_code == 409


async def test_web_initiated_consumed_by_bot_service(alice):
    me = (await alice.get("/api/auth/me")).json()
    async with get_sessionmaker()() as db:
        tok = await tglink_svc.create_web_initiated(db, await db.get(User, uuid.UUID(me["id"])))
        await db.commit()
        token = tok.token

        linked_user = await tglink_svc.confirm_web_initiated(
            db, token, tg_id=42, tg_username="ivan"
        )
        await db.commit()
        assert linked_user.tg_id == 42

    assert (await alice.get("/api/auth/me")).json()["tg_id"] == 42


async def test_status_reports_invalid_for_unknown_token(alice):
    r = await alice.get("/tg/link/does-not-exist/status")
    assert r.json() == {
        "valid": False,
        "kind": None,
        "tg_username": None,
        "reason": "ссылка не найдена",
    }
