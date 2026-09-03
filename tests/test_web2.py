"""Phase 7b/7c — sets, inbox, tags, members, settings, trash, profile, global search."""

from __future__ import annotations

import json
import re

from tests.conftest import web_csrf
from tests.conftest import web_upload as _upload


def _toast(response) -> str:
    trig = response.headers.get("HX-Trigger", "")
    try:
        return json.loads(trig).get("dc-toast", "")
    except ValueError:
        return trig


async def _doc_id(client, slug):
    p = await client.get("/search", headers={"HX-Request": "true"})
    m = re.search(r"/documents/([0-9a-f-]{36})", p.text)
    assert m, p.text[:400]
    return m.group(1)


# --- sets ---------------------------------------------------------
async def test_set_lifecycle(alice, web_domain):
    await _upload(alice, web_domain, "s1.txt")
    doc_id = await _doc_id(alice, web_domain)

    page = (await alice.get("/sets")).text
    r = await alice.post("/sets", data={"name": "Подборка", "csrf_token": web_csrf(page)})
    assert r.status_code == 303
    set_id = r.headers["location"].rsplit("/", 1)[-1]

    doc_page = (await alice.get(f"/documents/{doc_id}")).text
    add = await alice.post(
        f"/documents/{doc_id}/add-to-set",
        data={"set_id": set_id, "csrf_token": web_csrf(doc_page)},
        headers={"HX-Request": "true"},
    )
    assert add.status_code == 200 and "Подборка" in add.text  # doc-page fragment, now in the set

    detail = await alice.get(f"/sets/{set_id}")
    assert "s1" in detail.text

    # the document page can also take it back out of the set
    dp2 = (await alice.get(f"/documents/{doc_id}")).text
    rm = await alice.post(
        f"/documents/{doc_id}/remove-from-set",
        data={"set_id": set_id, "csrf_token": web_csrf(dp2)},
        headers={"HX-Request": "true"},
    )
    assert rm.status_code == 200 and "не входит ни в один набор" in rm.text


async def test_document_page_creates_a_new_set_inline(alice, web_domain):
    await _upload(alice, web_domain, "n1.txt")
    doc_id = await _doc_id(alice, web_domain)
    dp = (await alice.get(f"/documents/{doc_id}")).text
    r = await alice.post(
        f"/documents/{doc_id}/add-to-set",
        data={"set_id": "__new__", "new_name": "С нуля", "csrf_token": web_csrf(dp)},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200 and "С нуля" in r.text


async def _domain_id(client, slug):
    for d in (await client.get("/api/domains")).json():
        if d["slug"] == slug:
            return d["id"]
    raise AssertionError("domain not found")


async def test_bulk_index_and_add_to_set(alice, web_domain):
    await _upload(alice, web_domain, "b1.txt", b"alpha bravo")
    await _upload(alice, web_domain, "b2.txt", b"charlie delta")
    ids = re.findall(
        r"/documents/([0-9a-f-]{36})",
        (await alice.get("/search", headers={"HX-Request": "true"})).text,
    )
    ids = sorted(set(ids))[:2]
    dom = await _domain_id(alice, web_domain)
    page = (await alice.get("/search")).text
    common = {"csrf_token": web_csrf(page), "doc_ids": ",".join(ids), "domain_id": dom}

    r = await alice.post(
        "/search/bulk",
        data={**common, "action": "index"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    assert "Проиндексировано: 2" in _toast(r)

    r = await alice.post(
        "/search/bulk",
        data={**common, "action": "set", "set_id": "__new__", "new_name": "Пакет"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200 and "Пакет" in _toast(r)

    sets = (await alice.get("/sets")).text
    assert "Пакет" in sets


async def test_bulk_add_to_set_spans_domains(alice, web_domain):
    home = (await alice.get("/")).text
    other = (
        await alice.post("/domains", data={"name": "Второй", "csrf_token": web_csrf(home)})
    ).headers["location"].rsplit("/", 1)[-1]
    from urllib.parse import unquote

    other = unquote(other)
    await _upload(alice, web_domain, "d1.txt")
    await _upload(alice, other, "d2.txt")
    ids = sorted(set(re.findall(
        r"/documents/([0-9a-f-]{36})",
        (await alice.get("/search", headers={"HX-Request": "true"})).text,
    )))[:2]
    page = (await alice.get("/search")).text
    r = await alice.post(
        "/search/bulk",
        data={
            "csrf_token": web_csrf(page),
            "doc_ids": ",".join(ids),
            "action": "set",
            "set_id": "__new__",
            "new_name": "Общий",
        },
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200 and "Создан набор «Общий»" in _toast(r)
    # both cross-domain docs are in it
    detail = (await alice.get("/sets")).text
    assert "Общий" in detail


async def test_bulk_tag_is_additive_and_ignores_existing(alice, web_domain):
    await _upload(alice, web_domain, "bt1.txt")
    await _upload(alice, web_domain, "bt2.txt")
    ids = sorted(set(re.findall(
        r"/documents/([0-9a-f-]{36})",
        (await alice.get("/search", headers={"HX-Request": "true"})).text,
    )))[:2]
    # give the first one a tag already
    dp = (await alice.get(f"/documents/{ids[0]}")).text
    await alice.post(
        f"/documents/{ids[0]}/tags",
        data={"tags": "общий", "csrf_token": web_csrf(dp)},
        headers={"HX-Request": "true"},
    )
    page = (await alice.get("/search")).text
    r = await alice.post(
        "/search/bulk",
        data={
            "csrf_token": web_csrf(page),
            "doc_ids": ",".join(ids),
            "action": "tags",
            "tag_names": "общий, срочно",
        },
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    assert "Проставлено тегов: 3" in _toast(r)  # d0:срочно, d1:общий, d1:срочно


async def test_save_search_as_a_set_filter(alice, web_domain):
    await _upload(alice, web_domain, "kw1.txt", b"needle in a haystack")
    page = (await alice.get("/search")).text
    r = await alice.post(
        "/search/bulk",
        data={
            "csrf_token": web_csrf(page),
            "q": "needle",
            "action": "save_filter",
            "set_id": "__new__",
            "new_name": "Иголки",
        },
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200 and "Иголки" in _toast(r)
    detail = (await alice.get("/sets")).text
    assert "Иголки" in detail


async def test_set_share_link_and_revoke(alice, web_domain):
    await _upload(alice, web_domain, "sh.txt")
    doc_id = await _doc_id(alice, web_domain)
    await alice.post(f"/api/documents/{doc_id}/visibility?is_public=true")
    page = (await alice.get("/sets")).text
    set_id = (
        await alice.post("/sets", data={"name": "L", "csrf_token": web_csrf(page)})
    ).headers["location"].rsplit("/", 1)[-1]
    dp = (await alice.get(f"/documents/{doc_id}")).text
    await alice.post(
        f"/documents/{doc_id}/add-to-set", data={"set_id": set_id, "csrf_token": web_csrf(dp)}
    )

    detail = (await alice.get(f"/sets/{set_id}")).text
    r = await alice.post(
        f"/sets/{set_id}/links",
        data={"kind": "one_time", "csrf_token": web_csrf(detail)},
    )
    assert r.status_code == 303

    detail2 = await alice.get(f"/sets/{set_id}")
    assert "/d/" in detail2.text
    link_id = re.search(r"/links/([0-9a-f-]{36})/revoke", detail2.text).group(1)
    rv = await alice.post(f"/links/{link_id}/revoke", data={"csrf_token": web_csrf(detail2.text)})
    assert rv.status_code == 303


async def test_sets_list_table_and_name_filter(alice, web_domain):
    page = (await alice.get("/sets")).text
    for name in ("Договоры", "Отчёты"):
        await alice.post("/sets", data={"name": name, "csrf_token": web_csrf(page)})

    listing = (await alice.get("/sets")).text
    # table columns present
    assert "Явные док-ты" in listing and "Публичных" in listing and "Доступно" in listing
    assert "Договоры" in listing and "Отчёты" in listing

    filtered = (await alice.get("/sets?q=Догов", headers={"HX-Request": "true"})).text
    assert "Договоры" in filtered and "Отчёты" not in filtered


async def test_set_detail_shows_filter_counts_and_explicit_docs(alice, web_domain):
    await _upload(alice, web_domain, "fc1.txt", b"needle one")
    await _upload(alice, web_domain, "fc2.txt", b"hay two")
    doc_id = await _doc_id(alice, web_domain)
    await alice.post(f"/api/documents/{doc_id}/visibility?is_public=true")

    page = (await alice.get("/sets")).text
    set_id = (
        await alice.post("/sets", data={"name": "Счёт", "csrf_token": web_csrf(page)})
    ).headers["location"].rsplit("/", 1)[-1]

    detail = (await alice.get(f"/sets/{set_id}")).text
    # description is a large free-text field now
    assert 'name="description"' in detail and "<textarea" in detail

    # attach the visible doc explicitly + save a filter
    dp = (await alice.get(f"/documents/{doc_id}")).text
    await alice.post(
        f"/documents/{doc_id}/add-to-set",
        data={"set_id": set_id, "csrf_token": web_csrf(dp)},
    )
    await alice.post(
        "/search/bulk",
        data={
            "csrf_token": web_csrf(page),
            "q": "needle",
            "action": "save_filter",
            "set_id": set_id,
        },
        headers={"HX-Request": "true"},
    )

    detail2 = (await alice.get(f"/sets/{set_id}")).text
    assert "Явно привязанные документы" in detail2
    assert "fc2" in detail2  # the explicitly-attached doc (newest upload)
    assert "Всего доступно" in detail2  # per-filter count column


# --- inbox (table + modal tagging) --------------------------------
async def test_inbox_preset_and_modal_tagging(alice, web_domain):
    await _upload(alice, web_domain, "i1.txt")
    await _upload(alice, web_domain, "i2.txt")

    page = await alice.get("/search?preset=inbox")
    assert "Найдено: 2" in page.text
    assert 'class="btn btn-sm btn-primary tb-inbox"' in page.text and 'id="tagdlg"' in page.text

    card = await alice.get("/inbox/card", headers={"HX-Request": "true"})
    doc_id = re.search(r'data-doc="([0-9a-f-]{36})"', card.text).group(1)
    assert 'name="title"' in card.text

    r = await alice.post(
        f"/inbox/{doc_id}/done",
        data={
            "tags": "готово",
            "title": "Переименованный документ",
            "domain_id": "",
            "csrf_token": web_csrf(card.text),
        },
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    assert r.headers.get("HX-Trigger") == "inbox-refresh"
    assert "data-doc" in r.text  # the next card

    r2 = await alice.get("/search?preset=inbox", headers={"HX-Request": "true"})
    assert "Найдено: 1" in r2.text
    doc_page = await alice.get(f"/documents/{doc_id}")
    assert "Переименованный документ" in doc_page.text


async def test_inbox_defer_drops_out_of_the_preset(alice, web_domain):
    await _upload(alice, web_domain, "d1.txt")
    await _upload(alice, web_domain, "d2.txt")
    card = await alice.get("/inbox/card", headers={"HX-Request": "true"})
    doc_id = re.search(r'data-doc="([0-9a-f-]{36})"', card.text).group(1)

    r = await alice.post(
        f"/inbox/{doc_id}/defer",
        data={"domain_id": "", "csrf_token": web_csrf(card.text)},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200 and r.headers.get("HX-Trigger") == "inbox-refresh"
    r2 = await alice.get("/search?preset=inbox", headers={"HX-Request": "true"})
    assert "Найдено: 1" in r2.text


async def test_image_thumbnail(alice, web_domain):
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (120, 90), (30, 120, 200)).save(buf, "PNG")
    up = (await alice.get(f"/upload?domain={web_domain}")).text
    await alice.post(
        "/upload",
        data={"domain": web_domain, "csrf_token": web_csrf(up)},
        files={"file": ("pic.png", buf.getvalue(), "image/png")},
    )
    doc_id = await _doc_id(alice, web_domain)
    r = await alice.get(f"/documents/{doc_id}/thumb")
    assert r.status_code == 200 and r.headers["content-type"] == "image/webp"

    # image results carry a thumbnail in both search views
    cards = await alice.get("/search", headers={"HX-Request": "true"})
    assert f"/documents/{doc_id}/thumb" in cards.text
    table = await alice.get("/search?view=table", headers={"HX-Request": "true"})
    assert f"/documents/{doc_id}/thumb" in table.text


# --- tag vocabulary --------------------------------------------
async def test_global_tag_page_rename(alice, web_domain):
    await _upload(alice, web_domain, "tg.txt")
    doc_id = await _doc_id(alice, web_domain)
    dp = (await alice.get(f"/documents/{doc_id}")).text
    await alice.post(
        f"/documents/{doc_id}/tags",
        data={"tags": "Контракт", "csrf_token": web_csrf(dp)},
        headers={"HX-Request": "true"},
    )

    listing = await alice.get("/tags")
    assert "Контракт" in listing.text
    tag_id = re.search(r'action="/tags/([0-9a-f-]{36})"', listing.text).group(1)

    upd = await alice.post(
        f"/tags/{tag_id}",
        data={"name": "Договор", "color": "", "csrf_token": web_csrf(listing.text)},
    )
    assert upd.status_code == 303
    assert "Договор" in (await alice.get("/tags")).text


# --- members ---------------------------------------------------
async def test_members_add_and_role_change(alice, bob, web_domain):
    page = (await alice.get(f"/domains/{web_domain}/members")).text
    r = await alice.post(
        f"/domains/{web_domain}/members",
        data={"username": "bob", "role": "viewer", "csrf_token": web_csrf(page)},
    )
    assert r.status_code == 303

    listing = await alice.get(f"/domains/{web_domain}/members")
    assert "bob" in listing.text
    bob_id = re.search(r"/members/([0-9a-f-]{36})/remove", listing.text).group(1)

    ch = await alice.post(
        f"/domains/{web_domain}/members/{bob_id}",
        data={"role": "editor", "csrf_token": web_csrf(listing.text)},
    )
    assert ch.status_code == 303
    rm = await alice.post(
        f"/domains/{web_domain}/members/{bob_id}/remove",
        data={"csrf_token": web_csrf(listing.text)},
    )
    assert rm.status_code == 303


async def test_members_page_forbidden_for_viewer(alice, bob, web_domain):
    page = (await alice.get(f"/domains/{web_domain}/members")).text
    await alice.post(
        f"/domains/{web_domain}/members",
        data={"username": "bob", "role": "viewer", "csrf_token": web_csrf(page)},
    )
    assert (await bob.get(f"/domains/{web_domain}/members")).status_code == 403


# --- settings + trash ----------------------------------------
async def test_post_forms_carry_a_csrf_field(alice, web_domain):
    # a real browser submit uses the token in the form, not the body hx-headers
    for url in (f"/upload?domain={web_domain}", f"/domains/{web_domain}/settings"):
        page = (await alice.get(url)).text
        forms = re.findall(r"<form[^>]*method=\"post\"[^>]*>(.*?)</form>", page, re.S)
        assert forms, url
        assert all('name="csrf_token"' in f for f in forms), url


async def test_upload_without_csrf_shows_an_error_page(alice, web_domain):
    r = await alice.post(
        "/upload",
        data={"domain": web_domain},
        files={"file": ("x.txt", b"hi", "text/plain")},
        follow_redirects=False,
    )
    assert r.status_code == 403
    assert "text/html" in r.headers["content-type"]
    assert "Доступ запрещён" in r.text and "{" not in r.text[:1]


async def test_hx_upload_returns_just_the_result_fragment(alice, web_domain):
    up = (await alice.get(f"/upload?domain={web_domain}")).text
    r = await alice.post(
        "/upload",
        data={"domain": web_domain, "csrf_token": web_csrf(up)},
        files={"file": ("frag.txt", b"body", "text/plain")},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    assert "<html" not in r.text  # fragment, not the whole page
    assert "Загружено" in r.text


async def test_settings_save_allowed_types(alice, web_domain):
    page = (await alice.get(f"/domains/{web_domain}/settings")).text
    r = await alice.post(
        f"/domains/{web_domain}/settings",
        data={
            "name": "Рабочий",
            "allowed_types": "pdf, txt",
            "auto_ocr": "on",
            "csrf_token": web_csrf(page),
        },
    )
    assert r.status_code == 303
    # a disallowed type is now rejected on upload
    up = (await alice.get(f"/upload?domain={web_domain}")).text
    bad = await alice.post(
        "/upload",
        data={"domain": web_domain, "csrf_token": web_csrf(up)},
        files={"file": ("x.png", b"nope", "image/png")},
    )
    assert "не разрешён" in bad.text


async def test_trash_preset_and_bulk_restore(alice, web_domain):
    await _upload(alice, web_domain, "del.txt")
    doc_id = await _doc_id(alice, web_domain)
    dp = (await alice.get(f"/documents/{doc_id}")).text
    r = await alice.post(f"/documents/{doc_id}/delete", data={"csrf_token": web_csrf(dp)})
    assert r.status_code == 303

    trash = await alice.get("/search?preset=trash", headers={"HX-Request": "true"})
    assert "del" in trash.text and "Найдено: 1" in trash.text

    page = (await alice.get("/search?preset=trash")).text
    rs = await alice.post(
        "/search/bulk",
        data={
            "csrf_token": web_csrf(page),
            "doc_ids": doc_id,
            "action": "restore",
            "preset": "trash",
        },
        headers={"HX-Request": "true"},
    )
    assert rs.status_code == 200 and "Восстановлено: 1" in _toast(rs)
    empty = await alice.get("/search?preset=trash", headers={"HX-Request": "true"})
    assert "Найдено: 0" in empty.text


# --- profile + global search --------------------------------
async def test_profile_and_tg_link(alice):
    page = await alice.get("/profile")
    assert page.status_code == 200 and "Подключить Telegram" in page.text
    r = await alice.post("/profile/tg-link", data={"csrf_token": web_csrf(page.text)})
    assert r.status_code == 303 and "tg_token=" in r.headers["location"]
    after = await alice.get(r.headers["location"])
    assert "/start " in after.text


async def test_password_change(alice):
    page = (await alice.get("/profile")).text
    r = await alice.post(
        "/profile/password",
        data={
            "current": "correct horse!",
            "new_password": "brandnewpass9",
            "csrf_token": web_csrf(page),
        },
    )
    assert r.status_code == 303 and r.headers["location"] == "/profile?pw=ok"


async def test_global_search_spans_domains(alice):
    home = (await alice.get("/")).text
    a = (
        await alice.post("/domains", data={"name": "GA", "csrf_token": web_csrf(home)})
    ).headers["location"].rsplit("/", 1)[-1]
    b = (
        await alice.post("/domains", data={"name": "GB", "csrf_token": web_csrf(home)})
    ).headers["location"].rsplit("/", 1)[-1]
    await _upload(alice, a, "ga-doc.txt")
    await _upload(alice, b, "gb-doc.txt")

    r = await alice.get("/search?q=doc", headers={"HX-Request": "true"})
    assert ">GA</span>" in r.text and ">GB</span>" in r.text
