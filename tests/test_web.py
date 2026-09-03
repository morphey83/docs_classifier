"""Phase 7a — the HTMX + Jinja web UI (auth, dashboard, web_domain, search, document)."""

from __future__ import annotations

import re

from tests.conftest import web_csrf
from tests.conftest import web_upload as _upload


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
            "password2": "hunter2hunter",
            "csrf_token": web_csrf(page),
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/upload"
    home = await client.get("/")
    assert home.status_code == 200 and "webby" in home.text


async def test_login_bad_csrf_rejected(client):
    r = await client.post(
        "/login", data={"login": "x", "password": "y", "csrf_token": "forged"}
    )
    assert "устарела" in r.text


async def test_logout_clears_session(alice):
    page = (await alice.get("/")).text
    r = await alice.post("/logout", data={"csrf_token": web_csrf(page)}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert (await alice.get("/", follow_redirects=False)).status_code == 303


# --- dashboard + web_domain -------------------------------------------
async def test_create_domain_and_overview(alice, web_domain):
    home = await alice.get("/")
    assert "Рабочий" in home.text

    # the domain card is a modal partial now — a plain GET bounces to the dashboard
    assert (await alice.get(f"/domains/{web_domain}")).status_code == 303
    page = await alice.get(f"/domains/{web_domain}", headers={"HX-Request": "true"})
    assert page.status_code == 200
    assert "Рабочий" in page.text and 'id="domainbody"' in page.text
    assert "<html" not in page.text


async def test_dashboard_table_has_counts_and_actions(alice, web_domain):
    from tests.conftest import web_upload

    await web_upload(alice, web_domain, "d1.txt")
    home = (await alice.get("/")).text
    assert "<table" in home and "Новый домен" in home
    # per-domain action links
    assert "/search?domain_id=" in home
    assert f"/upload?domain={web_domain}" in home
    assert f"/domains/{web_domain}/settings" in home  # owner → edit icon
    assert f"/domains/{web_domain}/delete" in home  # owner → delete icon
    assert "в очереди / всего" in home


async def test_profile_menu_present_when_authed(alice):
    home = (await alice.get("/")).text
    assert 'href="/profile"' in home and "Профиль" in home
    assert 'action="/logout"' in home


async def test_login_form_present_when_anonymous(client):
    r = await client.get("/login")
    assert 'action="/login"' in r.text and 'name="password"' in r.text


async def test_foreign_domain_is_404(alice, bob, web_domain):
    assert (await bob.get(f"/domains/{web_domain}")).status_code == 404


async def test_web_404_is_an_html_page_not_json(alice):
    r = await alice.get("/no/such/page")
    assert r.status_code == 404
    assert "text/html" in r.headers["content-type"]
    assert "Страница не найдена" in r.text


async def test_htmx_error_becomes_a_toast(alice, web_domain):
    doc_id = await _one_doc(alice, web_domain)
    r = await alice.post(
        f"/documents/{doc_id}/tags",
        data={"tags": "x", "csrf_token": "forged"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    assert r.headers.get("HX-Reswap") == "none"
    assert "dc-toast" in r.headers.get("HX-Trigger", "")


# --- search --------------------------------------------------------
async def test_search_page_and_htmx_partial(alice, web_domain):
    await _upload(alice, web_domain, "alpha.txt", b"alpha body")
    await _upload(alice, web_domain, "beta.txt", b"beta body")

    full = await alice.get("/search")
    assert full.status_code == 200
    assert "<html" in full.text and "Найдено: 2" in full.text

    partial = await alice.get("/search?q=alpha", headers={"HX-Request": "true"})
    assert "<html" not in partial.text  # just the results fragment
    assert "Найдено: 1" in partial.text and "alpha" in partial.text


async def test_search_sort(alice, web_domain):
    await _upload(alice, web_domain, "zeta.txt")
    await _upload(alice, web_domain, "alpha.txt")
    r = await alice.get("/search?sort=title&dir=asc", headers={"HX-Request": "true"})
    assert r.status_code == 200
    # "alpha" before "zeta" with an ascending title sort
    assert r.text.index("alpha") < r.text.index("zeta")
    # the sort control reflects the active option
    assert "сортировка: название" in r.text


async def test_search_tag_include_and_exclude(alice, web_domain):
    await _upload(alice, web_domain, "keep.txt", b"a")
    await _upload(alice, web_domain, "drop.txt", b"b")
    doms = (await alice.get("/api/domains")).json()
    dom = next(d["id"] for d in doms if d["slug"] == web_domain)
    items = (await alice.get(f"/api/domains/{dom}/documents")).json()["items"]
    docs = {d["title"]: d["id"] for d in items}
    await alice.patch(
        f"/api/documents/{docs['keep']}/tags", json={"tag_names": ["договор", "срочно"]}
    )
    await alice.patch(
        f"/api/documents/{docs['drop']}/tags", json={"tag_names": ["договор", "черновик"]}
    )

    both = await alice.get("/search?tags=договор", headers={"HX-Request": "true"})
    assert "Найдено: 2" in both.text

    r = await alice.get("/search?tags=договор,-черновик", headers={"HX-Request": "true"})
    assert "Найдено: 1" in r.text
    assert docs["keep"] in r.text and docs["drop"] not in r.text


async def test_search_page_has_the_tag_chip_editor(alice, web_domain):
    page = (await alice.get("/search")).text
    assert 'id="dc-tags"' in page
    assert 'id="dc-tags-value"' in page and 'name="tags"' in page
    # syntax help moved into «i» popovers; the inline label hint is gone
    assert 'data-help="q-help"' in page and 'data-help="tags-help"' in page
    assert "клик по тегу — исключить" not in page


async def test_search_domain_filter(alice):
    home = (await alice.get("/")).text
    a = (await alice.post("/domains", data={"name": "DF-A", "csrf_token": web_csrf(home)})
         ).headers["location"].rsplit("/", 1)[-1]
    b = (await alice.post("/domains", data={"name": "DF-B", "csrf_token": web_csrf(home)})
         ).headers["location"].rsplit("/", 1)[-1]
    await _upload(alice, a, "only-a.txt")
    await _upload(alice, b, "only-b.txt")

    da = (await alice.get("/api/domains")).json()
    a_id = next(x["id"] for x in da if x["slug"] == a)
    r = await alice.get(f"/search?domain_id={a_id}", headers={"HX-Request": "true"})
    assert "only-a" in r.text and "only-b" not in r.text


async def test_search_empty_status_param_ok(alice, web_domain):
    r = await alice.get("/search?status=&q=&type=", headers={"HX-Request": "true"})
    assert r.status_code == 200


async def test_search_type_filter_lists_existing_extensions(alice, web_domain):
    await _upload(alice, web_domain, "a.txt")
    await _upload(alice, web_domain, "b.md")
    page = (await alice.get("/search")).text
    # the type filter is a multi-select toggle group built from real extensions
    assert 'id="type-group"' in page and 'name="type"' in page
    assert 'value="txt"' in page and 'value="md"' in page
    assert 'value="image"' in page and 'value="text"' in page
    assert "Фасеты" not in page

    # multi-select: filter to two extensions at once
    hx = await alice.get(
        "/search?type=txt&type=md", headers={"HX-Request": "true"}
    )
    assert "Найдено: 2" in hx.text
    one = await alice.get("/search?type=txt", headers={"HX-Request": "true"})
    assert "Найдено: 1" in one.text


# --- document ----------------------------------------------------
async def _one_doc(alice, slug):
    await _upload(alice, slug, "doc.txt", b"hello world")
    partial = await alice.get("/search", headers={"HX-Request": "true"})
    return re.search(r"/documents/([0-9a-f-]{36})", partial.text).group(1)


async def test_document_page_and_tag_edit(alice, web_domain):
    doc_id = await _one_doc(alice, web_domain)

    page = await alice.get(f"/documents/{doc_id}")
    assert page.status_code == 200 and 'data-tagfield' in page.text
    assert 'name="tags"' in page.text

    r = await alice.post(
        f"/documents/{doc_id}/tags",
        data={"tags": "договор, срочно", "csrf_token": web_csrf(page.text)},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    assert "договор" in r.text and "срочно" in r.text
    assert "<html" not in r.text  # fragment only
    # the search behind the modal is asked to re-run its filter
    import json
    assert json.loads(r.headers["HX-Trigger"])["dc-results-refresh"] is True


async def test_document_index_button(alice, web_domain):
    doc_id = await _one_doc(alice, web_domain)
    page = await alice.get(f"/documents/{doc_id}")
    r = await alice.post(
        f"/documents/{doc_id}/index",
        data={"csrf_token": web_csrf(page.text)},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200 and "Переиндексировать" in r.text


async def test_document_tag_edit_needs_csrf(alice, web_domain):
    doc_id = await _one_doc(alice, web_domain)
    r = await alice.post(f"/documents/{doc_id}/tags", data={"tags": "x", "csrf_token": "no"})
    assert r.status_code == 403
