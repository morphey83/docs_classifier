"""Compact callback-data factories (Telegram caps callback_data at 64 bytes)."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData


class DomainCb(CallbackData, prefix="dm"):
    id: str  # domain uuid, or "none" to clear


class PageCb(CallbackData, prefix="pg"):
    page: int  # next /find results page (0-based)


class DocCb(CallbackData, prefix="d"):
    verb: str  # file | tags | title | ocr | index | set
    id: str  # document uuid


class SetPickCb(CallbackData, prefix="sp"):
    id: str  # set uuid — the target document is held in FSM state


class NewSetCb(CallbackData, prefix="sn"):
    doc: str  # document uuid to seed the new set with


class SetCb(CallbackData, prefix="s"):
    verb: str  # open | zip | file | link_perm | link_once
    id: str  # set uuid


class InboxCb(CallbackData, prefix="ib"):
    verb: str  # skip | done | notag
    id: str  # document uuid
