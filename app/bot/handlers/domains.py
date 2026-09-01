"""/domains — pick the current domain, create domains, manage members."""

from __future__ import annotations

import uuid

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import state as state_svc
from app.bot.callbacks import DomCb
from app.bot.flows import Domains
from app.bot.handlers._util import needs_link
from app.models import Document, User
from app.rbac import ASSIGNABLE_ROLES, Cap, Role
from app.services import domains as domains_svc

router = Router(name="domains")


async def _doc_count(db: AsyncSession, domain_id: uuid.UUID) -> int:
    return int(
        await db.scalar(
            select(func.count())
            .select_from(Document)
            .where(Document.domain_id == domain_id, Document.deleted_at.is_(None))
        )
        or 0
    )


async def _send_list(target: Message, db: AsyncSession, user: User) -> None:
    memberships = await domains_svc.list_memberships(db, user)
    current = await state_svc.current_domain_id(db, user.id)
    b = InlineKeyboardBuilder()
    for d, m in memberships:
        mark = "✓ " if d.id == current else ""
        b.button(text=f"{mark}{d.name} · {m.role}", callback_data=DomCb(verb="open", id=str(d.id)).pack())
    b.button(text="➕ Создать домен", callback_data="dom:new")
    b.adjust(1)
    text = "Ваши домены:" if memberships else "У вас пока нет доменов."
    await target.answer(text + "\nТекущий (✓) используется для загрузок.", reply_markup=b.as_markup())


@router.message(Command("domains", "domain"))
async def domains_cmd(message: Message, db: AsyncSession, user: User | None) -> None:
    if user is None:
        return await needs_link(message)
    await _send_list(message, db, user)


@router.callback_query(F.data == "dom:new")
async def domain_new(cb: CallbackQuery, state: FSMContext, user: User | None) -> None:
    if user is None:
        return await needs_link(cb)
    await state.set_state(Domains.create)
    await cb.answer()
    await cb.message.answer("Название нового домена:")


@router.message(Domains.create, F.text)
async def domain_create(
    message: Message, state: FSMContext, db: AsyncSession, user: User | None
) -> None:
    await state.clear()
    if user is None:
        return await needs_link(message)
    d = await domains_svc.create_domain(db, user, name=message.text.strip())
    await db.flush()
    await message.answer(f"✅ Домен «{d.name}» создан.")
    await _send_list(message, db, user)


async def _card(cb: CallbackQuery, db: AsyncSession, user: User, domain_id: uuid.UUID) -> None:
    row = await domains_svc.get_membership(db, domain_id, user.id)
    if row is None:
        return await cb.answer("Домен недоступен", show_alert=True)
    domain, member = row
    role = Role(member.role)
    caps = _caps(role)
    members = await domains_svc.list_members(db, domain_id)
    current = await state_svc.current_domain_id(db, user.id)

    b = InlineKeyboardBuilder()
    if domain_id != current:
        b.button(text="✓ Сделать текущим", callback_data=DomCb(verb="setcur", id=str(domain_id)).pack())
    b.button(text="👥 Участники", callback_data=DomCb(verb="members", id=str(domain_id)).pack())
    if Cap.manage in caps:
        b.button(text="✏️ Переименовать", callback_data=DomCb(verb="rename", id=str(domain_id)).pack())
        b.button(text="➕ Участник", callback_data=DomCb(verb="addmember", id=str(domain_id)).pack())
    if user.id != domain.owner_id:
        b.button(text="🚪 Покинуть домен", callback_data=DomCb(verb="leave", id=str(domain_id)).pack())
    b.button(text="◀ К списку", callback_data=DomCb(verb="list", id="-").pack())
    b.adjust(1)

    text = (
        f"<b>{domain.name}</b>\n"
        f"ваша роль: {role}\n"
        f"документов: {await _doc_count(db, domain_id)}\n"
        f"участников: {len(members)}"
        + (" · текущий" if domain_id == current else "")
    )
    await cb.answer()
    await cb.message.answer(text, reply_markup=b.as_markup(), parse_mode="HTML")


def _caps(role: Role) -> frozenset[Cap]:
    from app.rbac import ROLE_CAPS

    return ROLE_CAPS[role]


@router.callback_query(DomCb.filter(F.verb == "list"))
async def cb_list(cb: CallbackQuery, db: AsyncSession, user: User | None) -> None:
    if user is None:
        return await needs_link(cb)
    await cb.answer()
    await _send_list(cb.message, db, user)


@router.callback_query(DomCb.filter(F.verb == "open"))
async def cb_open(
    cb: CallbackQuery, callback_data: DomCb, db: AsyncSession, user: User | None
) -> None:
    if user is None:
        return await needs_link(cb)
    await _card(cb, db, user, uuid.UUID(callback_data.id))


@router.callback_query(DomCb.filter(F.verb == "setcur"))
async def cb_setcur(
    cb: CallbackQuery, callback_data: DomCb, db: AsyncSession, user: User | None
) -> None:
    if user is None:
        return await needs_link(cb)
    domain_id = uuid.UUID(callback_data.id)
    if await domains_svc.get_membership(db, domain_id, user.id) is None:
        return await cb.answer("Домен недоступен", show_alert=True)
    await state_svc.set_current_domain(db, user.id, domain_id)
    await cb.answer("Текущий домен обновлён")
    await _card(cb, db, user, domain_id)


@router.callback_query(DomCb.filter(F.verb == "members"))
async def cb_members(
    cb: CallbackQuery, callback_data: DomCb, db: AsyncSession, user: User | None
) -> None:
    if user is None:
        return await needs_link(cb)
    domain_id = uuid.UUID(callback_data.id)
    row = await domains_svc.get_membership(db, domain_id, user.id)
    if row is None:
        return await cb.answer("Домен недоступен", show_alert=True)
    members = await domains_svc.list_members(db, domain_id)
    lines = [f"👥 <b>{row[0].name}</b> — участники:"]
    for m, u in members:
        owner = " (владелец)" if u.id == row[0].owner_id else ""
        lines.append(f"• {u.username} — {m.role}{owner}")
    b = InlineKeyboardBuilder()
    b.button(text="◀ Назад", callback_data=DomCb(verb="open", id=str(domain_id)).pack())
    await cb.answer()
    await cb.message.answer("\n".join(lines), reply_markup=b.as_markup(), parse_mode="HTML")


@router.callback_query(DomCb.filter(F.verb == "rename"))
async def cb_rename(
    cb: CallbackQuery, callback_data: DomCb, state: FSMContext, db: AsyncSession, user: User | None
) -> None:
    if user is None:
        return await needs_link(cb)
    row = await domains_svc.get_membership(db, uuid.UUID(callback_data.id), user.id)
    if row is None or Cap.manage not in _caps(Role(row[1].role)):
        return await cb.answer("Недостаточно прав", show_alert=True)
    await state.set_state(Domains.rename)
    await state.update_data(domain=callback_data.id)
    await cb.answer()
    await cb.message.answer("Новое название домена:")


@router.message(Domains.rename, F.text)
async def do_rename(
    message: Message, state: FSMContext, db: AsyncSession, user: User | None
) -> None:
    data = await state.get_data()
    await state.clear()
    if user is None:
        return await needs_link(message)
    row = await domains_svc.get_membership(db, uuid.UUID(data["domain"]), user.id)
    if row is None or Cap.manage not in _caps(Role(row[1].role)):
        return await message.answer("Недостаточно прав.")
    row[0].name = message.text.strip()
    await db.flush()
    await message.answer(f"✅ Домен переименован в «{row[0].name}».")


@router.callback_query(DomCb.filter(F.verb == "addmember"))
async def cb_addmember(
    cb: CallbackQuery, callback_data: DomCb, state: FSMContext, db: AsyncSession, user: User | None
) -> None:
    if user is None:
        return await needs_link(cb)
    row = await domains_svc.get_membership(db, uuid.UUID(callback_data.id), user.id)
    if row is None or Cap.manage not in _caps(Role(row[1].role)):
        return await cb.answer("Недостаточно прав", show_alert=True)
    await state.set_state(Domains.add_member)
    await state.update_data(domain=callback_data.id)
    await cb.answer()
    roles = ", ".join(r.value for r in ASSIGNABLE_ROLES)
    await cb.message.answer(f"Отправьте: <логин> <роль>\nроли: {roles}")


@router.message(Domains.add_member, F.text)
async def do_addmember(
    message: Message, state: FSMContext, db: AsyncSession, user: User | None
) -> None:
    data = await state.get_data()
    await state.clear()
    if user is None:
        return await needs_link(message)
    parts = message.text.split()
    if len(parts) != 2 or parts[1] not in {r.value for r in ASSIGNABLE_ROLES}:
        return await message.answer("Нужно «логин роль». Попробуйте снова через ➕ Участник.")
    row = await domains_svc.get_membership(db, uuid.UUID(data["domain"]), user.id)
    if row is None or Cap.manage not in _caps(Role(row[1].role)):
        return await message.answer("Недостаточно прав.")
    target = await db.scalar(select(User).where(User.username == parts[0].lower()))
    if target is None:
        return await message.answer("Пользователь не найден.")
    try:
        await domains_svc.add_or_update_member(
            db, row[0], target, Role(parts[1]), actor=user
        )
    except domains_svc.DomainError as err:
        return await message.answer(str(err))
    await message.answer(f"✅ {target.username} добавлен как {parts[1]}.")


@router.callback_query(DomCb.filter(F.verb == "leave"))
async def cb_leave(
    cb: CallbackQuery, callback_data: DomCb, db: AsyncSession, user: User | None
) -> None:
    if user is None:
        return await needs_link(cb)
    domain_id = uuid.UUID(callback_data.id)
    row = await domains_svc.get_membership(db, domain_id, user.id)
    if row is None:
        return await cb.answer("Домен недоступен", show_alert=True)
    try:
        await domains_svc.remove_member(db, row[0], user.id)
    except domains_svc.DomainError as err:
        return await cb.answer(str(err), show_alert=True)
    cur = await state_svc.current_domain_id(db, user.id)
    if cur == domain_id:
        await state_svc.set_current_domain(db, user.id, None)
    await cb.answer("Вы вышли из домена")
    await _send_list(cb.message, db, user)
