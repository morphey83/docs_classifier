"""FSM states for the bot's short free-text prompts (MemoryStorage)."""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class Edit(StatesGroup):
    title = State()  # data: {"doc": <uuid>}
    tags = State()  # data: {"doc": <uuid>}
    inbox_tags = State()  # data: {"doc": <uuid>, "domain": <uuid>}
    new_set = State()  # data: {"doc": <uuid>}
