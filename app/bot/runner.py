"""Dispatcher wiring and the long-polling entrypoint."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage

from app.config import settings

log = logging.getLogger("app.bot")

_dp: Dispatcher | None = None


def build_dispatcher() -> Dispatcher:
    """Build (once) the dispatcher. Handler routers are module-level singletons,
    so this is memoised — repeated calls return the same instance."""
    global _dp
    if _dp is not None:
        return _dp

    from app.bot.handlers import build_router
    from app.bot.middleware import DbSessionMiddleware, LinkedUserMiddleware

    dp = Dispatcher(storage=MemoryStorage())
    for observer in (dp.message, dp.callback_query):
        observer.middleware(DbSessionMiddleware())
        observer.middleware(LinkedUserMiddleware())
    dp.include_router(build_router())
    _dp = dp
    return dp


async def _run() -> None:
    if not settings.bot_token:
        raise SystemExit("BOT_TOKEN is not set")
    bot = Bot(
        settings.bot_token,
        default=DefaultBotProperties(link_preview_is_disabled=True),
    )
    dp = build_dispatcher()
    me = await bot.get_me()
    log.info("bot @%s starting (long-polling)", me.username)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_run())
