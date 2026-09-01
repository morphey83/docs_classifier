"""Bidirectional Telegram <-> account linking (§8).

Two independent handshakes share one token table and one verification rule:
the side claiming a Telegram identity must act *from* that Telegram account
(consuming the token via `/start <token>`), and the side claiming a service
account must be an authenticated web session. A typed ``@username`` is never
accepted as proof anywhere.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import TgLinkToken, User
from app.util.time import as_aware, utcnow


class TgLinkError(ValueError):
    pass


async def _new_token(
    db: AsyncSession,
    *,
    tg_id: int | None = None,
    tg_username: str | None = None,
    account_id: uuid.UUID | None = None,
) -> TgLinkToken:
    tok = TgLinkToken(
        token=secrets.token_urlsafe(24),
        tg_id=tg_id,
        tg_username=tg_username,
        account_id=account_id,
        expires_at=utcnow() + timedelta(minutes=settings.tg_link_ttl_minutes),
    )
    db.add(tok)
    await db.flush()
    return tok


async def create_bot_initiated(
    db: AsyncSession, *, tg_id: int, tg_username: str | None
) -> TgLinkToken:
    """`/start` with no payload: the bot has a tg_id, wants a web account."""
    return await _new_token(db, tg_id=tg_id, tg_username=tg_username)


async def create_web_initiated(db: AsyncSession, user: User) -> TgLinkToken:
    """Profile page: the web has an account, wants a Telegram id."""
    return await _new_token(db, account_id=user.id)


async def peek(db: AsyncSession, token: str) -> TgLinkToken | None:
    """Non-raising lookup for the public status endpoint."""
    return await db.scalar(select(TgLinkToken).where(TgLinkToken.token == token))


async def _load_pending(db: AsyncSession, token: str) -> TgLinkToken:
    tok = await peek(db, token)
    if tok is None:
        raise TgLinkError("link not found")
    if tok.consumed_at is not None:
        raise TgLinkError("this link was already used")
    if as_aware(tok.expires_at) <= utcnow():
        raise TgLinkError("this link has expired")
    return tok


async def _not_linked_elsewhere(
    db: AsyncSession, tg_id: int, *, exclude_user_id: uuid.UUID | None = None
) -> None:
    q = select(User.id).where(User.tg_id == tg_id)
    if exclude_user_id is not None:
        q = q.where(User.id != exclude_user_id)
    if await db.scalar(q.limit(1)):
        raise TgLinkError("this Telegram account is already linked to a different account")


async def confirm_bot_initiated(db: AsyncSession, token: str, user: User) -> TgLinkToken:
    """Web session confirms a bot-created (tg_id-only) token."""
    tok = await _load_pending(db, token)
    if tok.tg_id is None or tok.account_id is not None:
        raise TgLinkError("this link isn't waiting for a web account")
    if user.tg_id is not None:
        raise TgLinkError("this account already has a linked Telegram — unlink it first")
    await _not_linked_elsewhere(db, tok.tg_id)
    user.tg_id = tok.tg_id
    tok.account_id = user.id
    tok.consumed_at = utcnow()
    await db.flush()
    return tok


async def confirm_web_initiated(
    db: AsyncSession, token: str, *, tg_id: int, tg_username: str | None
) -> User:
    """Bot confirms a web-created (account-only) token, using its own tg_id."""
    tok = await _load_pending(db, token)
    if tok.account_id is None or tok.tg_id is not None:
        raise TgLinkError("this link isn't waiting for Telegram")
    user = await db.get(User, tok.account_id)
    if user is None:
        raise TgLinkError("the account for this link no longer exists")
    if user.tg_id is not None:
        raise TgLinkError("this account already has a linked Telegram — unlink it first")
    await _not_linked_elsewhere(db, tg_id)
    user.tg_id = tg_id
    tok.tg_id = tg_id
    tok.tg_username = tg_username
    tok.consumed_at = utcnow()
    await db.flush()
    return user
