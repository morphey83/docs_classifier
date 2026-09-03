"""/sets — the user's own sets: download the public archive, get share links."""

from __future__ import annotations

import uuid

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, FSInputFile, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.bot.callbacks import SetCb
from app.bot.handlers._util import needs_link
from app.bot.keyboards import archive_delivery_kb, set_actions_kb
from app.models import DocumentSet, User
from app.services import docsets as svc
from app.util.urls import absolute_url

router = Router(name="sets")

_SEND_MAX = 50 * 1024 * 1024


async def _load(db: AsyncSession, user: User, set_id: uuid.UUID) -> DocumentSet:
    s = await svc.get_owned_set(db, set_id, user.id)
    if s is None:
        raise ValueError("Набор не найден.")
    return s


@router.message(Command("sets"))
async def list_sets(message: Message, db: AsyncSession, user: User | None) -> None:
    if user is None:
        return await needs_link(message)
    sets = await svc.list_sets(db, user.id)
    if not sets:
        return await message.answer("У вас пока нет наборов. Создайте их в веб-интерфейсе.")
    b = InlineKeyboardBuilder()
    for s in sets:
        b.button(text=s.name, callback_data=SetCb(verb="open", id=str(s.id)).pack())
    b.adjust(1)
    await message.answer("Ваши наборы:", reply_markup=b.as_markup())


@router.callback_query(SetCb.filter(F.verb == "open"))
async def open_set(
    cb: CallbackQuery, callback_data: SetCb, db: AsyncSession, user: User | None
) -> None:
    if user is None:
        return await needs_link(cb)
    try:
        s = await _load(db, user, uuid.UUID(callback_data.id))
    except ValueError as err:
        return await cb.answer(str(err), show_alert=True)
    await cb.answer()
    await cb.message.answer(f"Набор «{s.name}»", reply_markup=set_actions_kb(str(s.id)))


@router.callback_query(SetCb.filter(F.verb.in_({"zip", "file"})))
async def download_archive(
    cb: CallbackQuery, callback_data: SetCb, bot: Bot, db: AsyncSession, user: User | None
) -> None:
    if user is None:
        return await needs_link(cb)
    try:
        s = await _load(db, user, uuid.UUID(callback_data.id))
    except ValueError as err:
        return await cb.answer(str(err), show_alert=True)

    artifact, current = await svc.ensure_current_archive(db, None, s, requested_by=user.id)
    path = storage.set_archive_path(str(s.id))
    if not svc.archive_is_ready(artifact, current):
        return await cb.message.answer(
            "Готовлю архив — нажмите ещё раз через несколько секунд.",
            reply_markup=set_actions_kb(str(s.id)),
        )
    if artifact.item_count == 0:
        return await cb.answer("В наборе нет публичных документов.", show_alert=True)
    await cb.answer()
    size = path.stat().st_size
    if callback_data.verb == "zip" and size <= _SEND_MAX:
        await cb.message.answer(
            f"Архив готов ({size // 1024} КБ). Как отправить?",
            reply_markup=archive_delivery_kb(str(s.id)),
        )
        return
    if callback_data.verb == "file" and size <= _SEND_MAX:
        await bot.send_document(cb.message.chat.id, FSInputFile(path, filename=f"{s.name}.zip"))
        return
    await _make_link(cb, db, s, user, kind="one_time")


@router.callback_query(SetCb.filter(F.verb.in_({"link_perm", "link_once"})))
async def make_link(
    cb: CallbackQuery, callback_data: SetCb, db: AsyncSession, user: User | None
) -> None:
    if user is None:
        return await needs_link(cb)
    try:
        s = await _load(db, user, uuid.UUID(callback_data.id))
    except ValueError as err:
        return await cb.answer(str(err), show_alert=True)
    kind = "permanent" if callback_data.verb == "link_perm" else "one_time"
    await _make_link(cb, db, s, user, kind=kind)


async def _make_link(cb, db, s, user, *, kind) -> None:
    link = await svc.create_share_link(db, None, s=s, user=user, kind=kind)
    await cb.answer()
    label = "постоянная" if kind == "permanent" else "одноразовая"
    await cb.message.answer(f"Ссылка ({label}):\n{absolute_url(f'/d/{link.token}')}")
