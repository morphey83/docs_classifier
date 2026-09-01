"""Inline keyboard builders."""

from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot.callbacks import DocCb, InboxCb, NewSetCb, PageCb, SetCb, SetPickCb
from app.models import Document
from app.ocr import engine as ocr_engine
from app.rbac import Cap


def result_kb(doc: Document, caps: frozenset[Cap]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    did = str(doc.id)
    if Cap.download in caps:
        b.button(text="📄 Файл", callback_data=DocCb(verb="file", id=did).pack())
    if Cap.write in caps:
        b.button(text="🔖 Теги", callback_data=DocCb(verb="tags", id=did).pack())
        b.button(text="✏️ Название", callback_data=DocCb(verb="title", id=did).pack())
    if Cap.process in caps:
        if doc.ocr_at is None and ocr_engine.is_supported(doc.mime):
            b.button(text="🔍 OCR", callback_data=DocCb(verb="ocr", id=did).pack())
        if doc.indexed_at is None:
            b.button(text="📇 Индекс", callback_data=DocCb(verb="index", id=did).pack())
    if Cap.view in caps:
        b.button(text="➕ В набор", callback_data=DocCb(verb="set", id=did).pack())
    b.adjust(2)
    return b.as_markup()


def pager_kb(page: int, total_pages: int) -> InlineKeyboardMarkup | None:
    if total_pages <= 1:
        return None
    b = InlineKeyboardBuilder()
    if page > 0:
        b.button(text="◀", callback_data=PageCb(page=page - 1).pack())
    b.button(text=f"{page + 1}/{total_pages}", callback_data="noop")
    if page + 1 < total_pages:
        b.button(text="▶", callback_data=PageCb(page=page + 1).pack())
    b.adjust(3)
    return b.as_markup()


def set_pick_kb(sets: list[tuple[str, str]], doc_id: str) -> InlineKeyboardMarkup:
    """``sets`` is a list of (set_id, label)."""
    b = InlineKeyboardBuilder()
    for sid, label in sets:
        b.button(text=label, callback_data=SetPickCb(id=sid).pack())
    b.button(text="➕ Новый набор", callback_data=NewSetCb(doc=doc_id).pack())
    b.adjust(1)
    return b.as_markup()


def set_actions_kb(set_id: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⬇️ Скачать архив", callback_data=SetCb(verb="zip", id=set_id).pack())
    b.button(text="🔗 Постоянная ссылка", callback_data=SetCb(verb="link_perm", id=set_id).pack())
    b.button(text="🔗 Разовая ссылка", callback_data=SetCb(verb="link_once", id=set_id).pack())
    b.adjust(1)
    return b.as_markup()


def archive_delivery_kb(set_id: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📄 Файлом", callback_data=SetCb(verb="file", id=set_id).pack())
    b.button(text="🔗 Ссылкой", callback_data=SetCb(verb="link_once", id=set_id).pack())
    b.adjust(2)
    return b.as_markup()


def inbox_kb(doc_id: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Готово (без тегов)", callback_data=InboxCb(verb="notag", id=doc_id).pack())
    b.button(text="⏭ Пропустить", callback_data=InboxCb(verb="skip", id=doc_id).pack())
    b.button(text="⏹ Стоп", callback_data=InboxCb(verb="done", id=doc_id).pack())
    b.adjust(1)
    return b.as_markup()
