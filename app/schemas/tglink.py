"""Telegram account-linking schemas (§8)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class TgLinkCreateOut(BaseModel):
    token: str
    deep_link: str | None  # None if TELEGRAM_BOT_USERNAME isn't configured


class TgLinkStatusOut(BaseModel):
    valid: bool
    kind: Literal["bot", "web"] | None = None
    tg_username: str | None = None
    reason: str | None = None
