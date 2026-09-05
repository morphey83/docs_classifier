"""Phase 7b/7c — sets, inbox, tags, members, settings, trash, profile, global search."""

from __future__ import annotations

import json
import re
import uuid

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

    doc_page = (await alice.get(f"/documents/{doc_id}", headers={"HX-Request": "true"})).text
    add = await alice.post(
        f"/documents/{doc_id}/add-to-set",
        data={"set_id": set_id, "csrf_token": web_csrf(doc_page)},
        headers={"HX-Request": "true"},
    )
    assert add.status_code == 200 and "Подборка" in add.text  # doc-page fragment, now in the set

    detail = await alice.get(f"/sets/{set_id}")
    assert "s1" in detail.text

    # the document page can also take it back out of the set
    dp2 = (await alice.get(f"/documents/{doc_id}", headers={"HX-Request": "true"})).text
    rm = await alice.post(
        f"/documents/{doc_id}/remove-from-set",
        data={"set_id": set_id, "csrf_token": web_csrf(dp2)},
        headers={"HX-Request": "true"},
    )
    assert rm.status_code == 200 and "не входит ни в один набор" in rm.text


async def test_document_page_creates_a_new_set_inline(alice, web_domain):
    await _upload(alice, web_domain, "n1.txt")
    doc_id = await _doc_id(alice, web_domain)
    dp = (await alice.get(f"/documents/{doc_id}", headers={"HX-Request": "true"})).text
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
    dp = (await alice.get(f"/documents/{ids[0]}", headers={"HX-Request": "true"})).text
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


async def test_saved_filter_round_trips_an_excluded_tag(alice, web_domain):
    await _upload(alice, web_domain, "f.txt")
    page = (await alice.get("/search")).text
    r = await alice.post(
        "/search/bulk",
        data={
            "csrf_token": web_csrf(page),
            "tags": "договор,-черновик",
            "action": "save_filter",
            "set_id": "__new__",
            "new_name": "Без черновиков",
        },
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    sid = re.search(r"/sets/([0-9a-f-]{36})", (await alice.get("/sets")).text)
    body = (await alice.get(f"/sets/{sid.group(1)}", headers={"HX-Request": "true"})).text
    # the filter card renders the exclusion, and its "открыть" link carries -черновик
    assert "-черновик" in body


async def test_set_share_link_and_revoke(alice, web_domain):
    await _upload(alice, web_domain, "sh.txt")
    doc_id = await _doc_id(alice, web_domain)
    await alice.post(f"/api/documents/{doc_id}/visibility?is_public=true")
    page = (await alice.get("/sets")).text
    set_id = (
        await alice.post("/sets", data={"name": "L", "csrf_token": web_csrf(page)})
    ).headers["location"].rsplit("/", 1)[-1]
    dp = (await alice.get(f"/documents/{doc_id}", headers={"HX-Request": "true"})).text
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
    dp = (await alice.get(f"/documents/{doc_id}", headers={"HX-Request": "true"})).text
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
    doc_page = await alice.get(f"/documents/{doc_id}", headers={"HX-Request": "true"})
    assert "Переименованный документ" in doc_page.text


async def test_inbox_is_derived_from_tags_not_a_flag(alice, web_domain):
    await _upload(alice, web_domain, "t1.txt")
    doc_id = await _doc_id(alice, web_domain)

    assert "Найдено: 1" in (await alice.get("/search?preset=inbox")).text

    # tagging from the document card takes it out of «не размечено»
    dp = (await alice.get(f"/documents/{doc_id}", headers={"HX-Request": "true"})).text
    await alice.post(
        f"/documents/{doc_id}/tags",
        data={"tags": "договор", "csrf_token": web_csrf(dp)},
        headers={"HX-Request": "true"},
    )
    assert "Найдено: 0" in (await alice.get("/search?preset=inbox")).text
    assert (await alice.get("/api/documents/" + doc_id)).json()["status"] == "tagged"

    # clearing every tag puts it back
    await alice.post(
        f"/documents/{doc_id}/tags",
        data={"tags": "", "csrf_token": web_csrf(dp)},
        headers={"HX-Request": "true"},
    )
    assert "Найдено: 1" in (await alice.get("/search?preset=inbox")).text
    assert (await alice.get("/api/documents/" + doc_id)).json()["status"] == "inbox"


async def test_doc_tag_editor_accepts_newlines(alice, web_domain):
    await _upload(alice, web_domain, "nl.txt")
    doc_id = await _doc_id(alice, web_domain)
    dp = (await alice.get(f"/documents/{doc_id}", headers={"HX-Request": "true"})).text
    # the tag route still splits on newlines (a pasted list may carry them)
    await alice.post(
        f"/documents/{doc_id}/tags",
        data={"tags": "договор,\nсрочно\nважно", "csrf_token": web_csrf(dp)},
        headers={"HX-Request": "true"},
    )
    got = {t["name"] for t in (await alice.get(f"/api/documents/{doc_id}")).json()["tags"]}
    assert got == {"договор", "срочно", "важно"}


async def test_inbox_skip_moves_on_without_persisting(alice, web_domain):
    await _upload(alice, web_domain, "d1.txt")
    await _upload(alice, web_domain, "d2.txt")
    card = await alice.get("/inbox/card", headers={"HX-Request": "true"})
    first_id = re.search(r'data-doc="([0-9a-f-]{36})"', card.text).group(1)

    r = await alice.post(
        f"/inbox/{first_id}/skip",
        data={"domain_id": "", "skip": "", "csrf_token": web_csrf(card.text)},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200 and r.headers.get("HX-Trigger") == "inbox-refresh"
    second_id = re.search(r'data-doc="([0-9a-f-]{36})"', r.text).group(1)
    assert second_id != first_id
    # the skip carries forward as a hidden field, not a database row
    assert f'name="skip" value="{first_id}"' in r.text

    # skipping never touches the search preset — both documents still show
    both = await alice.get("/search?preset=inbox", headers={"HX-Request": "true"})
    assert "Найдено: 2" in both.text

    # and a fresh queue (no skip param) starts over, showing the first one again
    fresh = await alice.get("/inbox/card", headers={"HX-Request": "true"})
    assert f'data-doc="{first_id}"' in fresh.text


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

    # image results carry a thumbnail
    cards = await alice.get("/search", headers={"HX-Request": "true"})
    assert f"/documents/{doc_id}/thumb" in cards.text


# --- tag vocabulary --------------------------------------------
async def test_global_tag_page_names_are_read_only(alice, web_domain):
    await _upload(alice, web_domain, "tg.txt")
    doc_id = await _doc_id(alice, web_domain)
    dp = (await alice.get(f"/documents/{doc_id}", headers={"HX-Request": "true"})).text
    await alice.post(
        f"/documents/{doc_id}/tags",
        data={"tags": "Контракт", "csrf_token": web_csrf(dp)},
        headers={"HX-Request": "true"},
    )

    listing = await alice.get("/tags")
    assert "Контракт" in listing.text and "Редактирование списка тегов" in listing.text
    # names are fixed — no rename control, and the route is gone
    assert "/rename" not in listing.text
    assert (await alice.post(f"/tags/{uuid.uuid4()}/rename")).status_code in (404, 405)


async def test_search_result_tag_uses_its_colour(alice, web_domain):
    await _upload(alice, web_domain, "col.txt")
    doc_id = await _doc_id(alice, web_domain)
    dp = (await alice.get(f"/documents/{doc_id}", headers={"HX-Request": "true"})).text
    await alice.post(
        f"/documents/{doc_id}/tags",
        data={"tags": "срочно", "csrf_token": web_csrf(dp)},
        headers={"HX-Request": "true"},
    )
    listing = (await alice.get("/tags")).text
    tag_id = re.search(r"/tags/([0-9a-f-]{36})/color", listing).group(1)
    await alice.post(
        f"/tags/{tag_id}/color",
        data={"color": "#d63939", "csrf_token": web_csrf(listing)},
        headers={"HX-Request": "true"},
    )

    results = (await alice.get("/search?q=col", headers={"HX-Request": "true"})).text
    assert "background:#d63939" in results


# --- members (domain modal tab) --------------------------------
async def test_members_add_and_role_change(alice, bob, web_domain):
    hx = {"HX-Request": "true"}
    page = (await alice.get(f"/domains/{web_domain}/members", headers=hx)).text
    r = await alice.post(
        f"/domains/{web_domain}/members",
        data={"username": "bob", "role": "viewer", "csrf_token": web_csrf(page)},
        headers=hx,
    )
    assert r.status_code == 200 and "bob" in r.text
    bob_id = re.search(r"/members/([0-9a-f-]{36})/remove", r.text).group(1)

    ch = await alice.post(
        f"/domains/{web_domain}/members/{bob_id}",
        data={"role": "editor", "csrf_token": web_csrf(r.text)},
        headers=hx,
    )
    assert ch.status_code == 200
    rm = await alice.post(
        f"/domains/{web_domain}/members/{bob_id}/remove",
        data={"csrf_token": web_csrf(r.text)},
        headers=hx,
    )
    assert rm.status_code == 200


async def test_members_page_forbidden_for_viewer(alice, bob, web_domain):
    hx = {"HX-Request": "true"}
    page = (await alice.get(f"/domains/{web_domain}/members", headers=hx)).text
    await alice.post(
        f"/domains/{web_domain}/members",
        data={"username": "bob", "role": "viewer", "csrf_token": web_csrf(page)},
        headers=hx,
    )
    # a plain (non-HX) request — the HX error handler would soften a 403 to a 200 toast
    assert (await bob.get(f"/domains/{web_domain}/members")).status_code == 403


# --- settings + trash ----------------------------------------
async def test_post_forms_carry_a_csrf_field(alice, web_domain):
    # a real browser submit uses the token in the form, not the body hx-headers
    for url in ("/", f"/upload?domain={web_domain}"):
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
    hx = {"HX-Request": "true"}
    page = (await alice.get(f"/domains/{web_domain}/settings", headers=hx)).text
    r = await alice.post(
        f"/domains/{web_domain}/settings",
        data={
            "name": "Рабочий",
            "allowed_types": "pdf, txt",
            "auto_ocr": "on",
            "csrf_token": web_csrf(page),
        },
        headers=hx,
    )
    assert r.status_code == 200 and "<html" not in r.text
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
    dp = (await alice.get(f"/documents/{doc_id}", headers={"HX-Request": "true"})).text
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


async def test_delete_and_restore_from_the_modal_close_and_refresh(alice, web_domain):
    await _upload(alice, web_domain, "md.txt")
    doc_id = await _doc_id(alice, web_domain)
    dp = (await alice.get(f"/documents/{doc_id}", headers={"HX-Request": "true"})).text

    dele = await alice.post(
        f"/documents/{doc_id}/delete",
        data={"csrf_token": web_csrf(dp)},
        headers={"HX-Request": "true"},
    )
    assert dele.status_code == 204
    trig = json.loads(dele.headers["HX-Trigger"])
    assert trig["dc-detail-close"] and trig["dc-search-refresh"]

    tp = (await alice.get("/search?preset=trash")).text
    resto = await alice.post(
        f"/documents/{doc_id}/restore",
        data={"csrf_token": web_csrf(tp)},
        headers={"HX-Request": "true"},
    )
    assert resto.status_code == 204
    assert json.loads(resto.headers["HX-Trigger"])["dc-search-refresh"]


# --- domain overview (modal) ---------------------------------------
async def test_domain_overview_is_a_modal_partial(alice, web_domain):
    di = await _domain_id(alice, web_domain)
    r = await alice.get(f"/domains/{web_domain}", headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert 'id="domainbody"' in r.text and "<html" not in r.text
    assert f"/search?domain_id={di}&preset=inbox" in r.text
    assert f"/search?domain_id={di}&preset=active" in r.text
    assert "Корзина" not in r.text


async def test_domain_rename_from_the_modal(alice, web_domain):
    hx = {"HX-Request": "true"}
    page = (await alice.get(f"/domains/{web_domain}", headers=hx)).text
    r = await alice.post(
        f"/domains/{web_domain}/rename",
        data={"name": "Переименован", "csrf_token": web_csrf(page)},
        headers=hx,
    )
    assert r.status_code == 200 and "Переименован" in r.text
    assert "Переименован" in (await alice.get("/")).text


async def test_tag_merge_is_scoped_to_owned_domains(alice, bob, web_domain):
    await _upload(alice, web_domain, "mine.txt")
    my_doc = await _doc_id(alice, web_domain)
    bob_dom = (await bob.post("/api/domains", json={"name": "BobDom"})).json()["id"]
    bd = (
        await bob.post(
            f"/api/domains/{bob_dom}/uploads",
            files={"file": ("b.txt", b"b", "text/plain")},
        )
    ).json()["document"]["id"]
    await alice.patch(f"/api/documents/{my_doc}/tags", json={"tag_names": ["old", "keep"]})
    await bob.patch(f"/api/documents/{bd}/tags", json={"tag_names": ["old", "keep"]})

    tags = {t["name"]: t["id"] for t in (await alice.get("/api/tags/all")).json()}
    r = await alice.post(f"/api/tags/{tags['old']}/merge", json={"into": tags["keep"]})
    assert r.status_code == 200

    mine = [t["name"] for t in (await alice.get(f"/api/documents/{my_doc}")).json()["tags"]]
    assert mine == ["keep"]  # merged away on alice's own document
    his = sorted(t["name"] for t in (await bob.get(f"/api/documents/{bd}")).json()["tags"])
    assert his == ["keep", "old"]  # bob's document is untouched
    assert "old" in {t["name"] for t in (await alice.get("/api/tags/all")).json()}


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
    da = next(d["id"] for d in (await alice.get("/api/domains")).json() if d["slug"] == a)
    db_ = next(d["id"] for d in (await alice.get("/api/domains")).json() if d["slug"] == b)
    # both domains show as clickable filter badges in the results
    assert f'data-domain="{da}"' in r.text and f'data-domain="{db_}"' in r.text
    assert "GA" in r.text and "GB" in r.text
