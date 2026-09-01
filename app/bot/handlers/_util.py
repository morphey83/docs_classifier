"""Small helpers shared by handlers."""

from __future__ import annotations

from aiogram.types import CallbackQuery, Message

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
