"""Phase 6b — drive a few updates through the real dispatcher + middleware chain."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest_asyncio
from aiogram import Bot
from aiogram.methods import TelegramMethod
from aiogram.types import (
    CallbackQuery,
    Chat,
    Message,
    Update,
)
from aiogram.types import (
    User as TgUser,
)

from app.bot.runner import build_dispatcher
from app.db import get_sessionmaker
from app.models import User

TG_ID = 424242


class RecordingBot(Bot):
    """A Bot that never touches the network — records the API calls made."""

    def __init__(self) -> None:
        super().__init__("42:AA-fake-token-for-tests-only-xxxxxxxxxxxxx")
        self.calls: list[TelegramMethod] = []

    async def __call__(self, method: TelegramMethod, request_timeout: int | None = None):
        self.calls.append(method)
        name = type(method).__name__
        if name in ("SendMessage", "SendDocument", "SendPhoto", "EditMessageText"):
            return Message(
                message_id=len(self.calls),
                date=datetime.now(tz=UTC),
                chat=Chat(id=1, type="private"),
                from_user=TgUser(id=1, is_bot=True, first_name="bot"),
            )
        return True

    def texts(self) -> list[str]:
        return [getattr(m, "text", "") or "" for m in self.calls]

    def button_texts(self) -> list[str]:
        out: list[str] = []
        for m in self.calls:
            markup = getattr(m, "reply_markup", None)
            for row in getattr(markup, "inline_keyboard", []) or []:
                out += [btn.text for btn in row]
        return out


@pytest_asyncio.fixture
async def linked(alice):
    """Link alice's account to a fake Telegram id, return (client, user_id)."""
    me = (await alice.get("/api/auth/me")).json()
    async with get_sessionmaker()() as db:
        u = await db.get(User, uuid.UUID(me["id"]))
        u.tg_id = TG_ID
        await db.commit()
    return alice, uuid.UUID(me["id"])


def _message_update(text: str, uid: int = TG_ID) -> Update:
    return Update(
        update_id=1,
        message=Message(
            message_id=1,
            date=datetime.now(tz=UTC),
            chat=Chat(id=uid, type="private"),
            from_user=TgUser(id=uid, is_bot=False, first_name="Alice", username="alice"),
            text=text,
        ),
    )


async def test_start_for_unlinked_user_sends_link(alice):
    bot, dp = RecordingBot(), build_dispatcher()
    await dp.feed_update(bot, _message_update("/start", uid=999001))
    assert any("/tg/link/" in t for t in bot.texts())


async def test_help_lists_commands(linked):
    bot, dp = RecordingBot(), build_dispatcher()
    await dp.feed_update(bot, _message_update("/help"))
    assert any("/find" in t and "/sets" in t for t in bot.texts())


async def test_find_without_query_prompts(linked):
    bot, dp = RecordingBot(), build_dispatcher()
    await dp.feed_update(bot, _message_update("/find"))
    assert any("договор" in t.lower() for t in bot.texts())


async def test_find_returns_results_across_domains(linked):
    alice, _uid = linked
    a = (await alice.post("/api/domains", json={"name": "Alpha"})).json()
    b = (await alice.post("/api/domains", json={"name": "Beta"})).json()
    await alice.post(
        f"/api/domains/{a['id']}/uploads",
        files={"file": ("contract-alpha.txt", b"alpha contract", "text/plain")},
    )
    await alice.post(
        f"/api/domains/{b['id']}/uploads",
        files={"file": ("contract-beta.txt", b"beta contract", "text/plain")},
    )

    bot, dp = RecordingBot(), build_dispatcher()
    await dp.feed_update(bot, _message_update("/find contract"))
    joined = "\n".join(bot.texts())
    assert "[Alpha]" in joined and "[Beta]" in joined
    assert "Найдено: 2" in joined


async def test_find_sends_image_results_as_photos(linked):
    from io import BytesIO

    from PIL import Image

    alice, _uid = linked
    d = (await alice.post("/api/domains", json={"name": "Снимки"})).json()
    buf = BytesIO()
    Image.new("RGB", (64, 48), (10, 90, 160)).save(buf, "PNG")
    await alice.post(
        f"/api/domains/{d['id']}/uploads",
        files={"file": ("shot-plan.png", buf.getvalue(), "image/png")},
    )

    bot, dp = RecordingBot(), build_dispatcher()
    await dp.feed_update(bot, _message_update("/find plan"))
    kinds = [type(m).__name__ for m in bot.calls]
    assert "SendPhoto" in kinds  # the image result is a photo card, not plain text
    captions = [getattr(m, "caption", "") or "" for m in bot.calls]
    assert any("shot-plan" in c for c in captions)


async def test_domain_command_lists_memberships(linked):
    alice, _ = linked
    await alice.post("/api/domains", json={"name": "OnlyOne"})
    bot, dp = RecordingBot(), build_dispatcher()
    await dp.feed_update(bot, _message_update("/domains"))
    labels = bot.button_texts()
    assert any("OnlyOne" in t for t in labels)
    assert any("Создать домен" in t for t in labels)


def _callback_update(data: str, uid: int = TG_ID) -> Update:
    return Update(
        update_id=2,
        callback_query=CallbackQuery(
            id="cb1",
            from_user=TgUser(id=uid, is_bot=False, first_name="Alice", username="alice"),
            chat_instance="ci",
            data=data,
            message=Message(
                message_id=5,
                date=datetime.now(tz=UTC),
                chat=Chat(id=uid, type="private"),
                from_user=TgUser(id=1, is_bot=True, first_name="bot"),
                text="…",
            ),
        ),
    )


async def test_create_domain_from_bot(linked):
    alice, _ = linked
    bot, dp = RecordingBot(), build_dispatcher()
    await dp.feed_update(bot, _callback_update("dom:new"))
    await dp.feed_update(bot, _message_update("Личный архив"))
    assert any("создан" in t for t in bot.texts())

    listed = (await alice.get("/api/domains")).json()
    assert any(d["name"] == "Личный архив" for d in listed)


async def test_notify_account_linked_noop_without_token():
    from app.services.tglink import notify_account_linked

    await notify_account_linked(123, "alice")  # BOT_TOKEN unset in tests → no-op, no raise
