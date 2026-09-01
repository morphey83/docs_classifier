"""/domain — pick the current domain for uploads."""

from __future__ import annotations

import uuid

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import state as state_svc
from app.bot.callbacks import DomainCb
from app.bot.handlers._util import needs_link
from app.models import User
from app.services import domains as domains_svc

router = Router(name="domains")


@router.message(Command("domain"))
async def choose_domain(message: Message, db: AsyncSession, user: User | None) -> None:
    if user is None:
        await needs_link(message)
        return
    memberships = await domains_svc.list_memberships(db, user)
    if not memberships:
        await message.answer("У вас нет доменов — создайте домен в веб-интерфейсе.")
        return
    current = await state_svc.current_domain_id(db, user.id)
    b = InlineKeyboardBuilder()
    for d, m in memberships:
        mark = "✓ " if d.id == current else ""
        b.button(text=f"{mark}{d.name} · {m.role}", callback_data=DomainCb(id=str(d.id)).pack())
    b.button(text="✖ Не выбирать", callback_data=DomainCb(id="none").pack())
    b.adjust(1)
    await message.answer(
        "Текущий домен для загрузок (поиск /find работает по всем доменам):",
        reply_markup=b.as_markup(),
    )


@router.callback_query(DomainCb.filter())
async def set_domain(
    cb: CallbackQuery, callback_data: DomainCb, db: AsyncSession, user: User | None
) -> None:
    if user is None:
        await needs_link(cb)
        return
    if callback_data.id == "none":
        await state_svc.set_current_domain(db, user.id, None)
        await cb.answer("Домен сброшен")
        if cb.message:
            await cb.message.edit_text("Домен не выбран.")
        return
    domain_id = uuid.UUID(callback_data.id)
    row = await domains_svc.get_membership(db, domain_id, user.id)
    if row is None:
        await cb.answer("Домен недоступен", show_alert=True)
        return
    await state_svc.set_current_domain(db, user.id, domain_id)
    await cb.answer("Готово")
    if cb.message:
        await cb.message.edit_text(f"Текущий домен: {row[0].name}")
