"""User registration, authentication, and session lifecycle."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Session, User
from app.security import (
    DUMMY_HASH,
    hash_password,
    needs_rehash,
    new_session_token,
    session_expiry,
    username_or_email_taken,
    verify_password,
)


class RegistrationError(ValueError):
    pass


async def register_user(db: AsyncSession, *, username: str, email: str, password: str) -> User:
    if await username_or_email_taken(db, username, email.lower()):
        raise RegistrationError("username or email already registered")
    user = User(
        username=username,
        email=email.lower(),
        password_hash=await hash_password(password),
    )
    db.add(user)
    await db.flush()

    # Every new user gets a personal domain to start with (architecture §2.1).
    from app.services.domains import create_domain

    await create_domain(db, user, name="Мои документы")
    return user


async def authenticate(db: AsyncSession, *, login: str, password: str) -> User | None:
    login = login.strip().lower()
    result = await db.execute(select(User).where((User.username == login) | (User.email == login)))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        await verify_password(DUMMY_HASH, password)  # constant-ish timing
        return None
    if not await verify_password(user.password_hash, password):
        return None
    if needs_rehash(user.password_hash):
        user.password_hash = await hash_password(password)
    return user


async def create_session(
    db: AsyncSession, user: User, *, user_agent: str | None, ip: str | None
) -> Session:
    session = Session(
        id=new_session_token(),
        user_id=user.id,
        expires_at=session_expiry(),
        user_agent=(user_agent or "")[:400] or None,
        ip=ip,
    )
    db.add(session)
    await db.flush()
    return session


async def delete_session(db: AsyncSession, token: str) -> None:
    session = await db.get(Session, token)
    if session is not None:
        await db.delete(session)
