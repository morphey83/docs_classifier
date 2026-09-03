import io
import zipfile

import pytest_asyncio


@pytest_asyncio.fixture
async def domain(alice):
    return (await alice.post("/api/domains", json={"name": "P2"})).json()


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


async def test_archive_upload_extracts_into_inbox(alice, domain):
    d = domain["id"]
    archive = _zip({"a.txt": b"alpha", "sub/b.txt": b"bravo", "sub/": b""})
    r = await alice.post(
        f"/api/domains/{d}/uploads", files={"file": ("pack.zip", archive, "application/zip")}
    )
    assert r.status_code == 202, r.text
    batch_id = r.json()["id"]

    # background task ran before the response returned
    detail = await alice.get(f"/api/domains/{d}/uploads/{batch_id}")
    b = detail.json()
    assert b["status"] == "done"
    assert b["item_count"] == 2
    assert {i["entry_name"] for i in b["items"]} == {"a.txt", "sub/b.txt"}

    docs = await alice.get(f"/api/domains/{d}/documents")
    assert docs.json()["total"] == 2
    assert all(x["source"] == "archive" for x in docs.json()["items"])


async def test_archive_name_conflict_is_skipped(alice, domain):
    d = domain["id"]
    await alice.post(f"/api/domains/{d}/uploads", files={"file": ("x.txt", b"first", "text/plain")})
    archive = _zip({"x.txt": b"different", "y.txt": b"new"})
    r = await alice.post(
        f"/api/domains/{d}/uploads", files={"file": ("p.zip", archive, "application/zip")}
    )
    b = (await alice.get(f"/api/domains/{d}/uploads/{r.json()['id']}")).json()
    assert b["status"] == "done"
    assert b["conflict_count"] == 1
    assert b["item_count"] == 1
    outcomes = {i["entry_name"]: i["outcome"] for i in b["items"]}
    assert outcomes == {"x.txt": "skipped", "y.txt": "created"}


async def test_archive_bomb_entry_count(alice, domain, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "max_archive_entries", 3)
    archive = _zip({f"f{i}.txt": b"x" for i in range(10)})
    r = await alice.post(
        f"/api/domains/{domain['id']}/uploads",
        files={"file": ("big.zip", archive, "application/zip")},
    )
    b = (await alice.get(f"/api/domains/{domain['id']}/uploads/{r.json()['id']}")).json()
    assert b["status"] == "failed"
    assert "3" in (b["error"] or "")


async def test_index_and_fulltext_search(alice, domain):
    d = domain["id"]
    doc = (
        await alice.post(
            f"/api/domains/{d}/uploads",
            files={"file": ("memo.txt", "договор поставки оборудования".encode(), "text/plain")},
        )
    ).json()["document"]

    # not searchable by body yet
    assert (await alice.get(f"/api/domains/{d}/documents?q=оборудования")).json()["total"] == 0

    idx = await alice.post(f"/api/documents/{doc['id']}/index")
    assert idx.json()["index_status"] == "done"
    assert idx.json()["text_source"] == "parsed"
    assert idx.json()["indexed_at"] is not None

    hit = await alice.get(f"/api/domains/{d}/documents?q=оборудования")
    assert [x["id"] for x in hit.json()["items"]] == [doc["id"]]

    assert (await alice.get(f"/api/domains/{d}/documents?has_index=true")).json()["total"] == 1
    assert (await alice.get(f"/api/domains/{d}/documents?has_index=false")).json()["total"] == 0


async def test_search_is_case_insensitive_for_cyrillic(alice, domain):
    d = domain["id"]
    doc = (
        await alice.post(
            f"/api/domains/{d}/uploads",
            files={
                "file": (
                    "Выписка из Реестра повесток.txt",
                    "Текст про РЕЕСТР и повестки".encode(),
                    "text/plain",
                )
            },
        )
    ).json()["document"]
    await alice.post(f"/api/documents/{doc['id']}/index")

    # title match, different case from the stored «Реестра»
    for q in ("реестр", "ВЫПИСКА", "Повесток"):
        hit = await alice.get(f"/api/domains/{d}/documents?q={q}")
        assert [x["id"] for x in hit.json()["items"]] == [doc["id"]], q

    # body-text match, also case-folded
    body = await alice.get(f"/api/domains/{d}/documents?q=реестр")
    assert body.json()["total"] == 1


async def test_search_filters_and_facets(alice, domain):
    d = domain["id"]
    uploads = [("a.pdf", "application/pdf"), ("b.txt", "text/plain"), ("c.txt", "text/plain")]
    for name, ct in uploads:
        r = await alice.post(f"/api/domains/{d}/uploads", files={"file": (name, name.encode(), ct)})
        did = r.json()["document"]["id"]
        if name == "b.txt":
            await alice.patch(f"/api/documents/{did}/tags", json={"tag_names": ["invoice"]})

    txt = await alice.get(f"/api/domains/{d}/documents?ext=txt")
    assert txt.json()["total"] == 2

    tagged = await alice.get(f"/api/domains/{d}/documents?tags=invoice")
    assert tagged.json()["total"] == 1

    facets = (await alice.get(f"/api/domains/{d}/documents")).json()["facets"]
    # tagging b.txt took it out of the inbox — «не размечено» = «нет тегов»
    assert facets["status"]["inbox"] == 2
    assert facets["status"]["tagged"] == 1
    mimes = {t["mime"]: t["count"] for t in facets["types"]}
    assert mimes == {"application/pdf": 1, "text/plain": 2}
    assert {t["name"] for t in facets["tags"]} == {"invoice"}


async def test_export_builds_zip_with_manifest(alice, domain):
    d = domain["id"]
    for i in range(2):
        await alice.post(
            f"/api/domains/{d}/uploads",
            files={"file": (f"doc{i}.txt", f"body {i}".encode(), "text/plain")},
        )

    exp = await alice.post(f"/api/domains/{d}/exports", json={})
    assert exp.status_code == 202
    art_id = exp.json()["id"]

    art = (await alice.get(f"/api/artifacts/{art_id}")).json()
    assert art["status"] == "ready"
    assert art["item_count"] == 2

    dl = await alice.get(f"/api/artifacts/{art_id}/download")
    assert dl.status_code == 200
    with zipfile.ZipFile(io.BytesIO(dl.content)) as zf:
        names = zf.namelist()
        assert "manifest.json" in names and "manifest.csv" in names
        assert sum(1 for n in names if n.startswith("files/")) == 2


async def test_export_by_id_list(alice, domain):
    d = domain["id"]
    ids = []
    for i in range(3):
        r = await alice.post(
            f"/api/domains/{d}/uploads",
            files={"file": (f"e{i}.txt", f"e{i}".encode(), "text/plain")},
        )
        ids.append(r.json()["document"]["id"])

    exp = await alice.post(f"/api/domains/{d}/exports", json={"document_ids": ids[:2]})
    art = (await alice.get(f"/api/artifacts/{exp.json()['id']}")).json()
    assert art["item_count"] == 2


async def test_artifact_hidden_from_non_member(alice, bob, domain):
    d = domain["id"]
    await alice.post(f"/api/domains/{d}/uploads", files={"file": ("z.txt", b"z", "text/plain")})
    art_id = (await alice.post(f"/api/domains/{d}/exports", json={})).json()["id"]
    assert (await bob.get(f"/api/artifacts/{art_id}")).status_code == 404
