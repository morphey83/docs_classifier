"""Password hashing, session tokens, and auth dependencies."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.db import get_session
from app.models import Session, User

_hasher = PasswordHasher()
# A real hash, used to keep authenticate() timing ~constant for unknown users.
DUMMY_HASH = _hasher.hash("x")


async def hash_password(password: str) -> str:
    return await run_in_threadpool(_hasher.hash, password)


async def verify_password(hashed: str, password: str) -> bool:
    def _check() -> bool:
        try:
            return _hasher.verify(hashed, password)
        except Argon2Error:
            return False

    return await run_in_threadpool(_check)


def needs_rehash(hashed: str) -> bool:
    try:
        return _hasher.check_needs_rehash(hashed)
    except Argon2Error:
        return True


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def session_expiry() -> datetime:
    return datetime.now(UTC) + timedelta(hours=settings.session_ttl_hours)


def _as_aware(dt: datetime) -> datetime:
    """SQLite drops tzinfo; treat a naive value as UTC."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


async def get_current_user(
    request: Request, db: AsyncSession = Depends(get_session)
) -> User:
    token = request.cookies.get(settings.session_cookie_name)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authenticated")
    session = await db.get(Session, token)
    if session is None or _as_aware(session.expires_at) <= datetime.now(UTC):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "session expired")
    user = await db.get(User, session.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "account disabled")
    return user


async def get_current_user_optional(
    request: Request, db: AsyncSession = Depends(get_session)
) -> User | None:
    try:
        return await get_current_user(request, db)
    except HTTPException:
        return None


async def username_or_email_taken(db: AsyncSession, username: str, email: str) -> bool:
    result = await db.execute(
        select(User.id).where((User.username == username) | (User.email == email))
    )
    return result.first() is not None
