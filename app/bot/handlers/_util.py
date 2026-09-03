"""Small helpers shared by handlers."""

from __future__ import annotations

from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

# Telegram rejects photos whose file is much bigger than this; past it we fall
# back to a plain text card.
_PREVIEW_MAX = 9 * 1024 * 1024

_LINK_HINT = (
    "Аккаунт не привязан. Отправьте /start, чтобы связать этот Telegram "
    "с аккаунтом DocsClassifier."
)


async def needs_link(event: Message | CallbackQuery) -> None:
    if isinstance(event, CallbackQuery):
        await event.answer(_LINK_HINT, show_alert=True)
    else:
        await event.answer(_LINK_HINT)


async def reply(event: Message | CallbackQuery, text: str, **kwargs) -> None:
    """Answer a message, or edit the message behind a callback, uniformly."""
    if isinstance(event, CallbackQuery):
        await event.answer()
        if event.message is not None:
            await event.message.answer(text, **kwargs)
    else:
        await event.answer(text, **kwargs)


async def send_doc_card(
    target: Message, doc, text: str, *, reply_markup: InlineKeyboardMarkup | None = None
) -> None:
    """Send a document card: as a photo with caption when the doc is a
    previewable image (thumbnail, or the original if it's small enough),
    otherwise a plain text message. Used for search results and the inbox."""
    from aiogram.types import FSInputFile

    from app import storage
    from app.services import thumbs

    if thumbs.can_thumb(doc.mime, doc.ext):
        thumb = await thumbs.ensure_thumb(doc.sha256)
        if thumb is not None:
            await target.answer_photo(FSInputFile(thumb), caption=text, reply_markup=reply_markup)
            return
        if doc.size_bytes <= _PREVIEW_MAX:
            try:
                async with storage.fetch_local(
                    storage.blobs_store(), storage.blob_key(doc.sha256)
                ) as original:
                    await target.answer_photo(
                        FSInputFile(original), caption=text, reply_markup=reply_markup
                    )
                    return
            except storage.ObjectNotFound:
                pass
    await target.answer(text, reply_markup=reply_markup)
