"""Phase 7a — the HTMX + Jinja web UI (auth, dashboard, domain, search, document)."""

from __future__ import annotations

import re

import pytest_asyncio


def _csrf(html: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', html) or re.search(
        r'"X-CSRF-Token": "([^"]+)"', html
    )
    assert m, "no CSRF token in page"
    return m.group(1)


@pytest_asyncio.fixture
async def domain(alice):
    page = (await alice.get("/")).text
    r = await alice.post("/domains", data={"name": "Рабочий", "csrf_token": _csrf(page)})
    assert r.status_code == 303
    slug = r.headers["location"].rsplit("/", 1)[-1]
    return slug


# --- auth ------------------------------------------------------------
async def test_login_page_public(client):
    r = await client.get("/login")
    assert r.status_code == 200 and "Вход" in r.text


async def test_dashboard_redirects_when_anonymous(client):
    r = await client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login?next=")


async def test_register_then_dashboard(client):
    page = (await client.get("/register")).text
    r = await client.post(
        "/register",
        data={
            "username": "webby",
            "email": "webby@example.com",
            "password": "hunter2hunter",
            "csrf_token": _csrf(page),
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    home = await client.get("/")
    assert home.status_code == 200 and "webby" in home.text


async def test_login_bad_csrf_rejected(client):
    r = await client.post(
        "/login", data={"login": "x", "password": "y", "csrf_token": "forged"}
    )
    assert "устарела" in r.text


async def test_logout_clears_session(alice):
    page = (await alice.get("/")).text
    r = await alice.post("/logout", data={"csrf_token": _csrf(page)}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert (await alice.get("/", follow_redirects=False)).status_code == 303


# --- dashboard + domain -------------------------------------------
async def test_create_domain_and_overview(alice, domain):
    home = await alice.get("/")
    assert "Рабочий" in home.text

    page = await alice.get(f"/domains/{domain}")
    assert page.status_code == 200
    assert "Рабочий" in page.text and "owner" in page.text


async def test_foreign_domain_is_404(alice, bob, domain):
    assert (await bob.get(f"/domains/{domain}")).status_code == 404


# --- search --------------------------------------------------------
async def _upload(client, slug, name, body):
    page = (await client.get(f"/domains/{slug}/upload")).text
    return await client.post(
        f"/domains/{slug}/upload",
        data={"csrf_token": _csrf(page)},
        files={"file": (name, body, "text/plain")},
    )


async def test_search_page_and_htmx_partial(alice, domain):
    await _upload(alice, domain, "alpha.txt", b"alpha body")
    await _upload(alice, domain, "beta.txt", b"beta body")

    full = await alice.get(f"/domains/{domain}/search")
    assert full.status_code == 200
    assert "<html" in full.text and "Найдено: 2" in full.text

    partial = await alice.get(
        f"/domains/{domain}/search?q=alpha", headers={"HX-Request": "true"}
    )
    assert "<html" not in partial.text  # just the results fragment
    assert "Найдено: 1" in partial.text and "alpha" in partial.text


async def test_search_empty_status_param_ok(alice, domain):
    r = await alice.get(
        f"/domains/{domain}/search?status=&q=&type=", headers={"HX-Request": "true"}
    )
    assert r.status_code == 200


# --- document ----------------------------------------------------
async def _one_doc(alice, slug):
    await _upload(alice, slug, "doc.txt", b"hello world")
    partial = await alice.get(f"/domains/{slug}/search", headers={"HX-Request": "true"})
    return re.search(r"/documents/([0-9a-f-]{36})", partial.text).group(1)


async def test_document_page_and_tag_edit(alice, domain):
    doc_id = await _one_doc(alice, domain)

    page = await alice.get(f"/documents/{doc_id}")
    assert page.status_code == 200 and "нет тегов" in page.text

    r = await alice.post(
        f"/documents/{doc_id}/tags",
        data={"tags": "договор, срочно", "csrf_token": _csrf(page.text)},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    assert "договор" in r.text and "срочно" in r.text
    assert "<html" not in r.text  # fragment only


async def test_document_index_button(alice, domain):
    doc_id = await _one_doc(alice, domain)
    page = await alice.get(f"/documents/{doc_id}")
    r = await alice.post(
        f"/documents/{doc_id}/index",
        data={"csrf_token": _csrf(page.text)},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200 and "done" in r.text


async def test_document_tag_edit_needs_csrf(alice, domain):
    doc_id = await _one_doc(alice, domain)
    r = await alice.post(f"/documents/{doc_id}/tags", data={"tags": "x", "csrf_token": "no"})
    assert r.status_code == 403
