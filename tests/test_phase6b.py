"""Phase 6b — bot pure helpers, persisted state, and the shared set-service."""

import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio

from app.bot import state as bot_state
from app.bot.formatting import human_size, result_line
from app.bot.parsing import ParsedQuery, describe, parse_query, to_filters
from app.db import get_sessionmaker
from app.models import DocStatus, User
from app.services import docsets as docsets_svc


# --- mini-syntax parser --------------------------------------------------
def test_parse_query_extracts_all_tokens():
    pq = parse_query("годовой отчёт #финансы #2024год type:pdf 2023 ocr:yes index:no status:tagged")
    assert pq.text == "годовой отчёт"
    assert pq.tags == ["финансы", "2024год"]
    assert pq.ext == "pdf"
    assert pq.year == 2023
    assert pq.has_ocr is True
    assert pq.has_index is False
    assert pq.status == "tagged"


def test_parse_query_plain_text_only():
    pq = parse_query("просто искать слова")
    assert pq == ParsedQuery(text="просто искать слова")


def test_parse_query_ignores_bad_year_and_status():
    pq = parse_query("12345 status:bogus 99")
    assert pq.year is None and pq.status is None
    assert pq.text == "12345 status:bogus 99"


def test_to_filters_maps_year_to_doc_date_range():
    f = to_filters(parse_query("x 2022 status:inbox #a"), page_size=5)
    assert f.q == "x"
    assert f.tags_all == ["a"]
    assert f.status == DocStatus.inbox
    assert f.doc_date_from == datetime(2022, 1, 1, tzinfo=UTC)
    assert f.doc_date_to == datetime(2022, 12, 31, 23, 59, 59, tzinfo=UTC)
    assert f.page == 1 and f.page_size == 5


def test_with_page_and_describe():
    pq = parse_query("договор #x type:pdf 2024 ocr:yes")
    assert pq.with_page(3).page == 3
    d = describe(pq)
    assert "«договор»" in d and "#x" in d and "тип:pdf" in d and "2024" in d and "распознан" in d


# --- formatting --------------------------------------------------------
@pytest.mark.parametrize(
    "n,expected",
    [(0, "0 Б"), (512, "512 Б"), (2048, "2.0 КБ"), (5 * 1024 * 1024, "5.0 МБ")],
)
def test_human_size(n, expected):
    assert human_size(n) == expected


class _FakeDoc:
    def __init__(self):
        self.title = "Договор №5"
        self.mime = "application/pdf"
        self.ext = "pdf"
        self.size_bytes = 2048
        self.doc_date = datetime(2024, 3, 1, tzinfo=UTC)
        self.ocr_at = datetime(2024, 4, 1, tzinfo=UTC)
        self.indexed_at = None


def test_result_line_has_domain_meta_tags_badges():
    line = result_line(_FakeDoc(), "Юрдок", ["контрагент", "2024"])
    assert line.startswith("[Юрдок] Договор №5")
    assert "PDF · 2024-03-01 · 2.0 КБ" in line
    assert "🔖 2024, контрагент" in line
    assert "распознан" in line


# --- persisted per-user bot state -------------------------------------
@pytest_asyncio.fixture
async def alice_user(alice):
    me = (await alice.get("/auth/me")).json()
    return uuid.UUID(me["id"])


async def test_state_roundtrip(alice_user):
    async with get_sessionmaker()() as db:
        assert await bot_state.current_domain_id(db, alice_user) is None
        did = uuid.uuid4()
        await bot_state.set_current_domain(db, alice_user, did)
        await bot_state.set_last_search(db, alice_user, {"raw": "договор #x"})
        await db.commit()

    async with get_sessionmaker()() as db:
        assert await bot_state.current_domain_id(db, alice_user) == did
        assert (await bot_state.last_search(db, alice_user)) == {"raw": "договор #x"}


async def test_clear_dangling_domain(alice, alice_user):
    d = (await alice.post("/domains", json={"name": "D"})).json()
    async with get_sessionmaker()() as db:
        await bot_state.set_current_domain(db, alice_user, uuid.UUID(d["id"]))
        await db.commit()
    # a domain the user is NOT a member of
    async with get_sessionmaker()() as db:
        await bot_state.set_current_domain(db, alice_user, uuid.uuid4())
        await db.commit()
    async with get_sessionmaker()() as db:
        await bot_state.clear_dangling_domain(db, alice_user)
        await db.commit()
        assert await bot_state.current_domain_id(db, alice_user) is None


# --- shared set service ----------------------------------------------
async def test_create_share_link_service_enforces_caps(alice, bob):
    d = (await alice.post("/domains", json={"name": "SL"})).json()
    bob_me = (await bob.get("/auth/me")).json()
    await alice.post(
        f"/domains/{d['id']}/members", json={"username": bob_me["username"], "role": "viewer"}
    )
    r = await alice.post(
        f"/domains/{d['id']}/uploads", files={"file": ("a.txt", b"a", "text/plain")}
    )
    doc_id = r.json()["document"]["id"]
    s = (
        await alice.post(
            f"/domains/{d['id']}/sets", json={"name": "s", "document_ids": [doc_id]}
        )
    ).json()

    from app.models import Domain
    from app.rbac import Role

    async with get_sessionmaker()() as db:
        domain = await db.get(Domain, uuid.UUID(d["id"]))
        set_obj = await db.get(docsets_svc.DocumentSet, uuid.UUID(s["id"]))
        bob_user = await db.get(User, uuid.UUID(bob_me["id"]))

        with pytest.raises(docsets_svc.SetError):
            await docsets_svc.create_share_link(
                db, None, domain=domain, set_obj=set_obj, user=bob_user,
                role=Role.viewer, kind="permanent",
            )
        link = await docsets_svc.create_share_link(
            db, None, domain=domain, set_obj=set_obj, user=bob_user,
            role=Role.viewer, kind="one_time",
        )
        assert link.max_downloads == 1
        await db.commit()


def test_dispatcher_builds():
    from app.bot.runner import build_dispatcher

    dp = build_dispatcher()
    names = {r.name for r in dp.sub_routers[0].sub_routers}
    assert {"start", "search", "sets", "inbox", "upload", "domains"} <= names
