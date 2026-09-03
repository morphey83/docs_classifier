"""/find — cross-domain search, paged results, and per-result actions."""

from __future__ import annotations

import math
import uuid

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.bot import state as state_svc
from app.bot.access import BotAccessError, doc_ctx, member_domain_ids, member_domain_names
from app.bot.callbacks import DocCb, NewSetCb, PageCb, SetPickCb
from app.bot.flows import Edit
from app.bot.formatting import result_line
from app.bot.handlers._util import needs_link, send_doc_card
from app.bot.keyboards import pager_kb, result_kb, set_pick_kb
from app.bot.parsing import describe, parse_query, to_filters
from app.config import settings
from app.jobs import dispatch
from app.models import DocumentTag, Tag, User
from app.ocr import engine as ocr_engine
from app.ocr.tasks import ocr_document
from app.rbac import Cap
from app.services import docsets as docsets_svc
from app.services import tags as tags_svc
from app.services.documents import update_document
from app.services.search import index_document, search_documents

router = Router(name="search")
PAGE = settings.bot_search_page_size

_SEND_MAX = 50 * 1024 * 1024


@router.message(Command("find"))
async def find(
    message: Message, command: CommandObject, db: AsyncSession, user: User | None
) -> None:
    if user is None:
        await needs_link(message)
        return
    raw = (command.args or "").strip()
    if not raw:
        stored = await state_svc.last_search(db, user.id)
        raw = (stored or {}).get("raw", "")
    if not raw:
        await message.answer("Например: /find договор #контрагент type:pdf 2024 ocr:yes")
        return
    await state_svc.set_last_search(db, user.id, {"raw": raw})
    await _run(message, db, user, parse_query(raw))


@router.callback_query(PageCb.filter())
async def paginate(
    cb: CallbackQuery, callback_data: PageCb, db: AsyncSession, user: User | None
) -> None:
    if user is None:
        await needs_link(cb)
        return
    stored = await state_svc.last_search(db, user.id)
    raw = (stored or {}).get("raw", "")
    await cb.answer()
    await _run(cb.message, db, user, parse_query(raw).with_page(callback_data.page))


@router.callback_query(F.data == "noop")
async def noop(cb: CallbackQuery) -> None:
    await cb.answer()


async def _run(target: Message, db: AsyncSession, user: User, pq) -> None:
    ids = await member_domain_ids(db, user)
    names = await member_domain_names(db, user)
    docs, total, _facets = await search_documents(db, ids, to_filters(pq, PAGE))
    if total == 0:
        await target.answer(f"Ничего не найдено · {describe(pq)}")
        return

    pages = max(1, math.ceil(total / PAGE))
    tag_map = await _tags_for(db, [d.id for d in docs])
    for doc in docs:
        _, _, caps = await _safe_caps(db, user, doc)
        await send_doc_card(
            target,
            doc,
            result_line(doc, names.get(doc.domain_id), tag_map.get(doc.id, [])),
            reply_markup=result_kb(doc, caps),
        )
    kb = pager_kb(pq.page, pages)
    await target.answer(f"Найдено: {total} · {describe(pq)}", reply_markup=kb)


async def _safe_caps(db, user, doc):
    try:
        return await doc_ctx(db, user, doc.id)
    except BotAccessError:
        return doc, None, frozenset()


async def _tags_for(db: AsyncSession, doc_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[str]]:
    if not doc_ids:
        return {}
    rows = await db.execute(
        select(DocumentTag.document_id, Tag.name)
        .join(Tag, Tag.id == DocumentTag.tag_id)
        .where(DocumentTag.document_id.in_(doc_ids))
    )
    out: dict[uuid.UUID, list[str]] = {}
    for did, name in rows:
        out.setdefault(did, []).append(name)
    return out


# --- per-result actions --------------------------------------------
@router.callback_query(DocCb.filter(F.verb == "file"))
async def send_file(
    cb: CallbackQuery, callback_data: DocCb, bot: Bot, db: AsyncSession, user: User | None
) -> None:
    if user is None:
        return await needs_link(cb)
    try:
        doc, _domain, _caps = await doc_ctx(
            db, user, uuid.UUID(callback_data.id), need=Cap.download
        )
    except BotAccessError as err:
        return await cb.answer(str(err), show_alert=True)
    if doc.size_bytes > _SEND_MAX:
        return await cb.answer("Файл слишком большой — откройте в веб.", show_alert=True)
    try:
        async with storage.fetch_local(
            storage.blobs_store(), storage.blob_key(doc.sha256)
        ) as path:
            await cb.answer("Отправляю…")
            await bot.send_document(
                cb.message.chat.id, FSInputFile(path, filename=doc.original_name)
            )
    except storage.ObjectNotFound:
        return await cb.answer("Файл отсутствует в хранилище.", show_alert=True)


@router.callback_query(DocCb.filter(F.verb == "ocr"))
async def request_ocr(
    cb: CallbackQuery, callback_data: DocCb, db: AsyncSession, user: User | None
) -> None:
    if user is None:
        return await needs_link(cb)
    try:
        doc, _d, _c = await doc_ctx(db, user, uuid.UUID(callback_data.id), need=Cap.process)
    except BotAccessError as err:
        return await cb.answer(str(err), show_alert=True)
    if not ocr_engine.is_supported(doc.mime):
        return await cb.answer("OCR не поддерживает этот тип.", show_alert=True)
    from app.models import OcrStatus

    doc.ocr_status = OcrStatus.pending
    await db.flush()
    await dispatch(None, "ocr_document", ocr_document, document_id=doc.id)
    await cb.answer("Отправлено на распознавание.")


@router.callback_query(DocCb.filter(F.verb == "index"))
async def request_index(
    cb: CallbackQuery, callback_data: DocCb, db: AsyncSession, user: User | None
) -> None:
    if user is None:
        return await needs_link(cb)
    try:
        doc, _d, _c = await doc_ctx(db, user, uuid.UUID(callback_data.id), need=Cap.process)
    except BotAccessError as err:
        return await cb.answer(str(err), show_alert=True)
    await index_document(db, doc)
    await cb.answer("Проиндексировано.")


@router.callback_query(DocCb.filter(F.verb == "title"))
async def ask_title(
    cb: CallbackQuery, callback_data: DocCb, state: FSMContext, db: AsyncSession, user: User | None
) -> None:
    if user is None:
        return await needs_link(cb)
    try:
        await doc_ctx(db, user, uuid.UUID(callback_data.id), need=Cap.write)
    except BotAccessError as err:
        return await cb.answer(str(err), show_alert=True)
    await state.set_state(Edit.title)
    await state.update_data(doc=callback_data.id)
    await cb.answer()
    await cb.message.answer("Отправьте новое название документа:")


@router.message(Edit.title, F.text)
async def set_title(
    message: Message, state: FSMContext, db: AsyncSession, user: User | None
) -> None:
    data = await state.get_data()
    await state.clear()
    if user is None:
        return await needs_link(message)
    try:
        doc, _d, _c = await doc_ctx(db, user, uuid.UUID(data["doc"]), need=Cap.write)
    except BotAccessError as err:
        return await message.answer(str(err))
    await update_document(db, doc, title=message.text.strip())
    await message.answer(f"✅ Название: «{doc.title}»")


@router.callback_query(DocCb.filter(F.verb == "tags"))
async def ask_tags(
    cb: CallbackQuery, callback_data: DocCb, state: FSMContext, db: AsyncSession, user: User | None
) -> None:
    if user is None:
        return await needs_link(cb)
    try:
        doc, _d, _c = await doc_ctx(db, user, uuid.UUID(callback_data.id), need=Cap.write)
    except BotAccessError as err:
        return await cb.answer(str(err), show_alert=True)
    current = await _tags_for(db, [doc.id])
    await state.set_state(Edit.tags)
    await state.update_data(doc=callback_data.id)
    await cb.answer()
    now = ", ".join(sorted(current.get(doc.id, []))) or "—"
    await cb.message.answer(
        f"Текущие теги: {now}\nОтправьте теги через запятую — добавлю к текущим "
        "(или «-» чтобы очистить все)."
    )


@router.message(Edit.tags, F.text)
async def set_tags(
    message: Message, state: FSMContext, db: AsyncSession, user: User | None
) -> None:
    data = await state.get_data()
    await state.clear()
    if user is None:
        return await needs_link(message)
    try:
        doc, _d, _c = await doc_ctx(db, user, uuid.UUID(data["doc"]), need=Cap.write)
    except BotAccessError as err:
        return await message.answer(str(err))

    raw = message.text.strip()
    if raw == "-":
        await tags_svc.set_document_tags(db, doc, [], actor=user)
        return await message.answer("🔖 Теги очищены.")

    names = [p.strip() for p in raw.split(",") if p.strip()]
    current_ids = set(
        await db.scalars(select(DocumentTag.tag_id).where(DocumentTag.document_id == doc.id))
    )
    for name in names:
        tag = await tags_svc.get_or_create_tag(db, doc.domain_id, name, actor=user)
        current_ids.add(tag.id)
    await tags_svc.set_document_tags(db, doc, list(current_ids), actor=user)
    applied = await _tags_for(db, [doc.id])
    await message.answer("🔖 " + (", ".join(sorted(applied.get(doc.id, []))) or "—"))


@router.callback_query(DocCb.filter(F.verb == "set"))
async def choose_set(
    cb: CallbackQuery, callback_data: DocCb, state: FSMContext, db: AsyncSession, user: User | None
) -> None:
    if user is None:
        return await needs_link(cb)
    try:
        doc, _domain, _c = await doc_ctx(db, user, uuid.UUID(callback_data.id), need=Cap.download)
    except BotAccessError as err:
        return await cb.answer(str(err), show_alert=True)
    sets = await docsets_svc.list_sets(db, user.id)
    await state.update_data(doc=callback_data.id)
    await cb.answer()
    rows = [(str(s.id), s.name) for s in sets]
    await cb.message.answer(
        f"В какой набор добавить «{doc.title}»?",
        reply_markup=set_pick_kb(rows, str(doc.id)),
    )


@router.callback_query(SetPickCb.filter())
async def add_to_set(
    cb: CallbackQuery,
    callback_data: SetPickCb,
    state: FSMContext,
    db: AsyncSession,
    user: User | None,
) -> None:
    if user is None:
        return await needs_link(cb)
    data = await state.get_data()
    doc_id = data.get("doc")
    if not doc_id:
        return await cb.answer("Начните заново.", show_alert=True)
    s = await docsets_svc.get_owned_set(db, uuid.UUID(callback_data.id), user.id)
    if s is None:
        return await cb.answer("Набор не найден.", show_alert=True)
    added = await docsets_svc.add_items(db, s, [uuid.UUID(doc_id)], actor=user)
    await state.clear()
    await cb.answer("Добавлено" if added else "Уже в наборе")
    await cb.message.answer(f"«{s.name}» обновлён. /sets")


@router.callback_query(NewSetCb.filter())
async def new_set_prompt(
    cb: CallbackQuery, callback_data: NewSetCb, state: FSMContext, user: User | None
) -> None:
    if user is None:
        return await needs_link(cb)
    await state.set_state(Edit.new_set)
    await state.update_data(doc=callback_data.doc)
    await cb.answer()
    await cb.message.answer("Название нового набора:")


@router.message(Edit.new_set, F.text)
async def new_set_create(
    message: Message, state: FSMContext, db: AsyncSession, user: User | None
) -> None:
    data = await state.get_data()
    await state.clear()
    if user is None:
        return await needs_link(message)
    try:
        doc, _domain, _c = await doc_ctx(db, user, uuid.UUID(data["doc"]), need=Cap.download)
    except BotAccessError as err:
        return await message.answer(str(err))
    s = await docsets_svc.create_set(
        db, user, name=message.text.strip(), document_ids=[doc.id]
    )
    await message.answer(f"✅ Набор «{s.name}» создан, документ добавлен. /sets")
