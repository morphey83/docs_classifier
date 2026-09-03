"""/inbox — walk the current domain's inbox, tagging as you go."""

from __future__ import annotations

import uuid

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import state as state_svc
from app.bot.callbacks import InboxCb
from app.bot.flows import Edit
from app.bot.formatting import result_line
from app.bot.handlers._util import needs_link, send_doc_card
from app.bot.keyboards import inbox_kb
from app.models import Domain, User
from app.rbac import Cap
from app.services import documents as docs_svc
from app.services import domains as domains_svc
from app.services import tags as tags_svc

router = Router(name="inbox")


async def _domain(db: AsyncSession, user: User) -> Domain | None:
    domain_id = await state_svc.current_domain_id(db, user.id)
    if domain_id is None:
        return None
    row = await domains_svc.get_membership(db, domain_id, user.id)
    return row[0] if row else None


@router.message(Command("inbox"))
async def inbox(message: Message, state: FSMContext, db: AsyncSession, user: User | None) -> None:
    if user is None:
        return await needs_link(message)
    domain = await _domain(db, user)
    if domain is None:
        return await message.answer("Сначала выберите домен: /domain")
    row = await domains_svc.get_membership(db, domain.id, user.id)
    from app.rbac import ROLE_CAPS, Role

    if Cap.write not in ROLE_CAPS[Role(row[1].role)]:
        return await message.answer("Нет права обрабатывать инбокс в этом домене.")
    await _next(message, state, db, domain, user)


async def _next(target, state: FSMContext, db, domain: Domain, user: User) -> None:
    doc = await docs_svc.next_inbox_document(db, domain.id, user.id)
    if doc is None:
        await state.clear()
        await target.answer("Инбокс пуст 🎉")
        return
    await state.set_state(Edit.inbox_tags)
    await state.update_data(doc=str(doc.id), domain=str(domain.id))

    text = (
        result_line(doc, domain.name, [])
        + "\n\nОтправьте теги через запятую, либо жмите кнопку:"
    )
    await send_doc_card(target, doc, text, reply_markup=inbox_kb(str(doc.id)))


@router.message(Edit.inbox_tags, F.text)
async def tag_and_advance(
    message: Message, state: FSMContext, db: AsyncSession, user: User | None
) -> None:
    if user is None:
        return await needs_link(message)
    data = await state.get_data()
    doc = await docs_svc.get_document(db, uuid.UUID(data["doc"]))
    domain = (await domains_svc.get_membership(db, uuid.UUID(data["domain"]), user.id))[0]
    if doc is None:
        return await _next(message, state, db, domain, user)

    names = [p.strip() for p in message.text.split(",") if p.strip()]
    tag_ids = await tags_svc.resolve_names(db, names, actor=user)
    await tags_svc.set_document_tags(db, doc, tag_ids, actor=user)
    await docs_svc.complete_document(db, doc)
    await message.answer(f"✅ «{doc.title}» — {', '.join(names) or 'без тегов'}")
    await _next(message, state, db, domain, user)


@router.callback_query(InboxCb.filter())
async def inbox_button(
    cb: CallbackQuery,
    callback_data: InboxCb,
    state: FSMContext,
    db: AsyncSession,
    user: User | None,
) -> None:
    if user is None:
        return await needs_link(cb)
    data = await state.get_data()
    if not data.get("domain"):
        return await cb.answer("Начните с /inbox", show_alert=True)
    domain = (await domains_svc.get_membership(db, uuid.UUID(data["domain"]), user.id))[0]
    doc = await docs_svc.get_document(db, uuid.UUID(callback_data.id))

    if callback_data.verb == "done":
        await state.clear()
        await cb.answer("Остановлено")
        return
    if doc is not None and callback_data.verb == "skip":
        await docs_svc.defer_document(db, doc, user.id)
        await cb.answer("Пропущено")
    elif doc is not None and callback_data.verb == "notag":
        await docs_svc.complete_document(db, doc)
        await cb.answer("Готово без тегов")
    await _next(cb.message, state, db, domain, user)
