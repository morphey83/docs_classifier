"""/start — account linking (both directions, §8) — plus /help."""

from __future__ import annotations

from html import escape

from aiogram import Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.menu import HELP, root_kb
from app.models import User
from app.services import tglink as tglink_svc
from app.util.urls import absolute_url

router = Router(name="start")


@router.message(CommandStart(deep_link=True))
async def start_deep_link(
    message: Message, command: CommandObject, db: AsyncSession, user: User | None
) -> None:
    token = (command.args or "").strip()
    try:
        linked = await tglink_svc.confirm_web_initiated(
            db,
            token,
            tg_id=message.from_user.id,
            tg_username=message.from_user.username,
        )
    except tglink_svc.TgLinkError as err:
        await message.answer(f"Не получилось привязать: {err}")
        return
    await message.answer(
        f"Готово — этот Telegram привязан к аккаунту «{linked.username}».\n\n{HELP}",
        reply_markup=root_kb(),
    )


@router.message(CommandStart())
async def start(message: Message, db: AsyncSession, user: User | None) -> None:
    if user is not None:
        await message.answer(
            f"Аккаунт «{user.username}» уже привязан.\n\n{HELP}", reply_markup=root_kb()
        )
        return
    tok = await tglink_svc.create_bot_initiated(
        db, tg_id=message.from_user.id, tg_username=message.from_user.username
    )
    url = absolute_url(f"/tg/link/{tok.token}")
    await message.answer(
        "Чтобы пользоваться ботом, привяжите аккаунт DocsClassifier:\n\n"
        f'👉 <a href="{escape(url, quote=True)}">Открыть страницу привязки</a>\n\n'
        f"Ссылка (для копирования): <code>{escape(url)}</code>\n"
        "Действует 15 минут. Войдите или зарегистрируйтесь и нажмите «Привязать».",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("help"))
async def help_cmd(message: Message, user: User | None) -> None:
    await message.answer(HELP, reply_markup=root_kb())
