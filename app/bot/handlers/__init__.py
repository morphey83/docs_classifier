"""Aiogram routers, aggregated in registration order."""

from __future__ import annotations

from aiogram import Router

from app.bot.handlers import domains, inbox, search, sets, start, upload


def build_router() -> Router:
    root = Router(name="root")
    root.include_routers(
        start.router,
        domains.router,
        sets.router,
        search.router,
        inbox.router,
        upload.router,
    )
    return root
