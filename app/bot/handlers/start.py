"""/start — account linking (both directions, §8) — plus /help."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User
from app.services import tglink as tglink_svc
from app.util.urls import absolute_url

router = Router(name="start")

_HELP = (
    "DocsClassifier — бот\n\n"
    "/domain — выбрать домен для загрузок\n"
    "Отправьте файл или архив — попадёт в инбокс выбранного домена\n"
    "/inbox — обработать инбокс (теги)\n"
    "/find <запрос> — поиск по всем доступным доменам\n"
    "   пример: /find договор #контрагент type:pdf 2024 ocr:yes\n"
    "/sets — наборы документов и архивы\n"
)


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
        f"Готово — этот Telegram привязан к аккаунту «{linked.username}».\n\n{_HELP}"
    )


@router.message(CommandStart())
async def start(message: Message, db: AsyncSession, user: User | None) -> None:
    if user is not None:
        await message.answer(f"Аккаунт «{user.username}» уже привязан.\n\n{_HELP}")
        return
    tok = await tglink_svc.create_bot_initiated(
        db, tg_id=message.from_user.id, tg_username=message.from_user.username
    )
    await message.answer(
        "Чтобы привязать аккаунт, откройте ссылку, войдите или зарегистрируйтесь "
        f"и подтвердите привязку:\n\n{absolute_url(f'/tg/link/{tok.token}')}\n\n"
        "Ссылка действует 15 минут."
    )


@router.message(Command("help"))
async def help_cmd(message: Message, user: User | None) -> None:
    await message.answer(_HELP)
