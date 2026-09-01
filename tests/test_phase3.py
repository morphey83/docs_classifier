import pytest
import pytest_asyncio

from app.ocr import engine as ocr_engine

PNG_HEADER = b"\x89PNG\r\n\x1a\n"


@pytest_asyncio.fixture
async def domain(alice):
    return (await alice.post("/domains", json={"name": "P3"})).json()


@pytest.fixture
def fake_ocr(monkeypatch):
    calls: list[tuple] = []

    def _run(path, mime, lang):
        calls.append((mime, lang))
        return ocr_engine.OcrResult(text="счёт на оплату услуг", sidecar_pdf=b"%PDF-1.4 fake")

    monkeypatch.setattr(ocr_engine, "run_ocr", _run)
    return calls


async def _upload_png(client, domain_id, name="scan.png"):
    data = PNG_HEADER + name.encode() + b"\x00" * 32
    r = await client.post(
        f"/domains/{domain_id}/uploads", files={"file": (name, data, "image/png")}
    )
    return r.json()["document"]


async def test_ocr_extracts_text_and_makes_searchable(alice, domain, fake_ocr):
    doc = await _upload_png(alice, domain["id"])
    assert doc["ocr_status"] == "none"

    r = await alice.post(f"/documents/{doc['id']}/ocr")
    assert r.status_code == 200, r.text
    assert r.json()["ocr_status"] == "pending"  # job is queued

    body = (await alice.get(f"/documents/{doc['id']}")).json()  # job has run
    assert body["ocr_status"] == "done"
    assert body["ocr_at"] is not None
    assert body["text_source"] == "ocr"
    assert body["index_status"] == "done"
    assert fake_ocr == [("image/png", "rus+eng")]

    hit = await alice.get(f"/domains/{domain['id']}/documents?q=оплату")
    assert [x["id"] for x in hit.json()["items"]] == [doc["id"]]


async def test_ocr_lang_override(alice, domain, fake_ocr):
    doc = await _upload_png(alice, domain["id"])
    await alice.post(f"/documents/{doc['id']}/ocr?lang=deu")
    assert fake_ocr[-1] == ("image/png", "deu")


async def test_ocr_unsupported_type_422(alice, domain):
    doc = (
        await alice.post(
            f"/domains/{domain['id']}/uploads",
            files={"file": ("a.txt", b"hello", "text/plain")},
        )
    ).json()["document"]
    r = await alice.post(f"/documents/{doc['id']}/ocr")
    assert r.status_code == 422
    doc2 = await alice.get(f"/documents/{doc['id']}")
    assert doc2.json()["ocr_status"] == "unsupported"


async def test_ocr_failure_marks_failed(alice, domain, monkeypatch):
    def _boom(path, mime, lang):
        raise RuntimeError("tesseract exploded")

    monkeypatch.setattr(ocr_engine, "run_ocr", _boom)
    doc = await _upload_png(alice, domain["id"])
    r = await alice.post(f"/documents/{doc['id']}/ocr")
    assert r.json()["ocr_status"] in ("pending", "failed")
    # background task has run
    assert (await alice.get(f"/documents/{doc['id']}")).json()["ocr_status"] == "failed"


async def test_auto_ocr_on_upload(alice, domain, fake_ocr):
    d = domain["id"]
    await alice.patch(f"/domains/{d}", json={"settings": {"auto_ocr": True}})
    doc = await _upload_png(alice, d)
    got = await alice.get(f"/documents/{doc['id']}")
    assert got.json()["ocr_status"] == "done"
    assert len(fake_ocr) == 1


async def test_has_ocr_filter(alice, domain, fake_ocr):
    d = domain["id"]
    a = await _upload_png(alice, d, "a.png")
    await _upload_png(alice, d, "b.png")
    await alice.post(f"/documents/{a['id']}/ocr")

    assert (await alice.get(f"/domains/{d}/documents?has_ocr=true")).json()["total"] == 1
    assert (await alice.get(f"/domains/{d}/documents?has_ocr=false")).json()["total"] == 1
