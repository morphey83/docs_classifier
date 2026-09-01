"""Build absolute links from the configured public host (app/config.py)."""

from __future__ import annotations

from app.config import settings


def absolute_url(path: str) -> str:
    """``path`` (starting with '/') made absolute via ``PUBLIC_BASE_URL``.

    Falls back to a relative path when the setting is unset — fine for a
    same-origin browser link, not enough for the bot to send in a message.
    """
    if not settings.public_base_url:
        return path
    return settings.public_base_url.rstrip("/") + path


def bot_deep_link(payload: str) -> str | None:
    """``https://t.me/<bot>?start=<payload>`` — ``None`` if the bot username isn't configured."""
    if not settings.telegram_bot_username:
        return None
    return f"https://t.me/{settings.telegram_bot_username}?start={payload}"
