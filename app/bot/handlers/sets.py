"""/sets — list sets, download the archive, get share links."""

from __future__ import annotations

import uuid

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, FSInputFile, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.bot.access import BotAccessError, domain_ctx, member_domain_names
from app.bot.callbacks import SetCb
from app.bot.formatting import set_line
from app.bot.handlers._util import needs_link
from app.bot.keyboards import archive_delivery_kb, set_actions_kb
from app.models import DocumentSet, SetVisibility, User
from app.services import docsets as svc
from app.util.urls import absolute_url

router = Router(name="sets")

_SEND_MAX = 50 * 1024 * 1024


async def _visible_sets(db: AsyncSession, user: User) -> list[DocumentSet]:
    names = await member_domain_names(db, user)
    if not names:
        return []
    rows = await db.scalars(
        select(DocumentSet)
        .where(
            DocumentSet.domain_id.in_(list(names)),
            (DocumentSet.visibility == SetVisibility.domain)
            | (DocumentSet.created_by == user.id),
        )
        .order_by(DocumentSet.updated_at.desc())
    )
    return list(rows)


async def _load(db: AsyncSession, user: User, set_id: uuid.UUID) -> DocumentSet:
    s = await db.get(DocumentSet, set_id)
    if s is None:
        raise BotAccessError("Набор не найден.")
    if s.created_by != user.id and s.visibility != SetVisibility.domain:
        raise BotAccessError("Набор недоступен.")
    await domain_ctx(db, user, s.domain_id)  # membership check
    return s


@router.message(Command("sets"))
async def list_sets(message: Message, db: AsyncSession, user: User | None) -> None:
    if user is None:
        return await needs_link(message)
    sets = await _visible_sets(db, user)
    if not sets:
        return await message.answer("У вас пока нет наборов. Добавляйте документы из /find.")
    names = await member_domain_names(db, user)
    b = InlineKeyboardBuilder()
    for s in sets:
        b.button(
            text=set_line(s.name, names.get(s.domain_id, "?"), s.item_count),
            callback_data=SetCb(verb="open", id=str(s.id)).pack(),
        )
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
    except BotAccessError as err:
        return await cb.answer(str(err), show_alert=True)
    await cb.answer()
    await cb.message.answer(
        f"Набор «{s.name}» · {s.item_count} док.", reply_markup=set_actions_kb(str(s.id))
    )


@router.callback_query(SetCb.filter(F.verb.in_({"zip", "file"})))
async def download_archive(
    cb: CallbackQuery, callback_data: SetCb, bot: Bot, db: AsyncSession, user: User | None
) -> None:
    if user is None:
        return await needs_link(cb)
    try:
        s = await _load(db, user, uuid.UUID(callback_data.id))
        domain, _role, caps = await domain_ctx(db, user, s.domain_id)
    except BotAccessError as err:
        return await cb.answer(str(err), show_alert=True)
    from app.rbac import Cap

    if Cap.download not in caps:
        return await cb.answer("Нужно право download.", show_alert=True)

    artifact, current = await svc.ensure_current_archive(
        db, None, domain, s, requested_by=user.id
    )
    path = storage.set_archive_path(str(s.id))
    if not svc.archive_is_ready(artifact, current):
        return await cb.message.answer(
            "Готовлю архив — нажмите «Скачать архив» ещё раз через несколько секунд.",
            reply_markup=set_actions_kb(str(s.id)),
        )
    await cb.answer()
    size = path.stat().st_size
    if callback_data.verb == "zip" and size <= _SEND_MAX:
        await cb.message.answer(
            f"Архив готов ({size // 1024} КБ). Как отправить?",
            reply_markup=archive_delivery_kb(str(s.id)),
        )
        return
    if callback_data.verb == "file" and size <= _SEND_MAX:
        await bot.send_document(
            cb.message.chat.id, FSInputFile(path, filename=f"{s.name}.zip")
        )
        return
    # too big -> a one-time link
    await _make_link(cb, db, domain, s, user, kind="one_time")


@router.callback_query(SetCb.filter(F.verb.in_({"link_perm", "link_once"})))
async def make_link(
    cb: CallbackQuery, callback_data: SetCb, db: AsyncSession, user: User | None
) -> None:
    if user is None:
        return await needs_link(cb)
    try:
        s = await _load(db, user, uuid.UUID(callback_data.id))
        domain, _role, _caps = await domain_ctx(db, user, s.domain_id)
    except BotAccessError as err:
        return await cb.answer(str(err), show_alert=True)
    kind = "permanent" if callback_data.verb == "link_perm" else "one_time"
    await _make_link(cb, db, domain, s, user, kind=kind)


async def _make_link(cb, db, domain, s, user, *, kind) -> None:
    _, role, _ = await domain_ctx(db, user, s.domain_id)
    try:
        link = await svc.create_share_link(
            db, None, domain=domain, set_obj=s, user=user, role=role, kind=kind
        )
    except svc.SetError as err:
        return await cb.answer(str(err), show_alert=True)
    await cb.answer()
    label = "постоянная" if kind == "permanent" else "одноразовая"
    await cb.message.answer(f"Ссылка ({label}):\n{absolute_url(f'/d/{link.token}')}")
