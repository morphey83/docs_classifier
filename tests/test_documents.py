import pytest_asyncio


@pytest_asyncio.fixture
async def domain(alice):
    return (await alice.post("/domains", json={"name": "Docs"})).json()


def _file(name="report.txt", content=b"hello world", ct="text/plain"):
    return {"file": (name, content, ct)}


async def test_upload_lands_in_inbox(alice, domain):
    r = await alice.post(f"/domains/{domain['id']}/uploads", files=_file())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["outcome"] == "created"
    doc = body["document"]
    assert doc["status"] == "inbox"
    assert doc["title"] == "report"
    assert doc["size_bytes"] == 11
    assert doc["mime"] == "text/plain"

    inbox = await alice.get(f"/domains/{domain['id']}/inbox")
    assert inbox.json()["count"] == 1


async def test_identical_content_is_deduplicated(alice, domain):
    a = await alice.post(f"/domains/{domain['id']}/uploads", files=_file())
    b = await alice.post(f"/domains/{domain['id']}/uploads", files=_file())
    assert b.json()["outcome"] == "deduplicated"
    assert a.json()["document"]["id"] == b.json()["document"]["id"]


async def test_name_conflict_then_replace_and_new(alice, domain):
    d = domain["id"]
    await alice.post(f"/domains/{d}/uploads", files=_file(content=b"v1"))
    conflict = await alice.post(f"/domains/{d}/uploads", files=_file(content=b"v2"))
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["error"] == "name_conflict"

    rep = await alice.post(f"/domains/{d}/uploads?on_conflict=replace", files=_file(content=b"v2"))
    assert rep.json()["outcome"] == "replaced"
    assert rep.json()["document"]["version"] == 2

    new = await alice.post(f"/domains/{d}/uploads?on_conflict=new", files=_file(content=b"v3"))
    assert new.json()["outcome"] == "new_from_conflict"
    assert new.json()["document"]["title"] == "report (2)"


async def test_tagging_and_complete_flow(alice, domain):
    d = domain["id"]
    doc = (await alice.post(f"/domains/{d}/uploads", files=_file())).json()["document"]

    tagged = await alice.patch(
        f"/documents/{doc['id']}/tags", json={"tag_names": ["Контракт", "2024"]}
    )
    assert tagged.status_code == 200
    names = sorted(t["name"] for t in tagged.json()["tags"])
    assert names == ["2024", "Контракт"]

    # created in the domain vocabulary
    vocab = await alice.get(f"/domains/{d}/tags")
    assert {t["name"] for t in vocab.json()} == {"Контракт", "2024"}
    assert all(t["usage_count"] == 1 for t in vocab.json())

    done = await alice.post(f"/documents/{doc['id']}/complete")
    assert done.json()["status"] == "tagged"
    assert (await alice.get(f"/domains/{d}/inbox")).json()["count"] == 0

    # filter by tag
    found = await alice.get(f"/domains/{d}/documents?tags=2024")
    assert [x["id"] for x in found.json()["items"]] == [doc["id"]]


async def test_inbox_next_and_defer(alice, domain):
    d = domain["id"]
    ids = []
    for i in range(3):
        r = await alice.post(
            f"/domains/{d}/uploads",
            files=_file(name=f"f{i}.txt", content=f"c{i}".encode()),
        )
        ids.append(r.json()["document"]["id"])
    first = (await alice.get(f"/domains/{d}/inbox/next")).json()
    assert first["id"] == ids[0]

    await alice.post(f"/documents/{first['id']}/defer")
    second = (await alice.get(f"/domains/{d}/inbox/next")).json()
    assert second["id"] == ids[1]

    cleared = await alice.post(f"/domains/{d}/inbox/undefer")
    assert cleared.json()["cleared"] == 1
    assert (await alice.get(f"/domains/{d}/inbox/next")).json()["id"] == ids[0]


async def test_download_returns_bytes(alice, domain):
    doc = (
        await alice.post(f"/domains/{domain['id']}/uploads", files=_file(content=b"PAYLOAD"))
    ).json()["document"]
    r = await alice.get(f"/documents/{doc['id']}/content")
    assert r.status_code == 200
    assert r.content == b"PAYLOAD"


async def test_quota_enforced(alice, domain, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "default_domain_quota_mb", 0)
    r = await alice.post(f"/domains/{domain['id']}/uploads", files=_file(content=b"x" * 2048))
    assert r.status_code == 413
    assert r.json()["detail"]["error"] == "quota_exceeded"


async def test_pdf_doc_date_extracted(alice, domain):
    import io

    from pypdf import PdfWriter

    w = PdfWriter()
    w.add_blank_page(width=72, height=72)
    w.add_metadata({"/CreationDate": "D:20200115103000Z"})
    buf = io.BytesIO()
    w.write(buf)

    r = await alice.post(
        f"/domains/{domain['id']}/uploads",
        files=_file("scan.pdf", buf.getvalue(), "application/pdf"),
    )
    doc = r.json()["document"]
    assert doc["mime"] == "application/pdf"
    assert doc["doc_date"] is not None and doc["doc_date"].startswith("2020-01-15")
