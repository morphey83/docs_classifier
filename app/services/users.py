"""User registration, authentication, and session lifecycle."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
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
from app.services.email import new_verify_code
from app.util.time import as_aware, utcnow


class RegistrationError(ValueError):
    pass


async def register_user(db: AsyncSession, *, username: str, email: str, password: str) -> User:
    if await username_or_email_taken(db, username, email.lower()):
        raise RegistrationError("username or email already registered")
    user = User(
        username=username,
        email=email.lower(),
        password_hash=await hash_password(password),
        # confirmation only gates login when SMTP is configured
        email_verified_at=None if settings.email_verification_enabled else utcnow(),
    )
    db.add(user)
    await db.flush()

    # Every new user gets a personal domain to start with (architecture §2.1).
    from app.services.domains import create_domain

    await create_domain(db, user, name="Мои документы")
    return user


def email_unverified(user: User) -> bool:
    """True when this account still has to confirm its address with a code."""
    return settings.email_verification_enabled and user.email_verified_at is None


async def issue_verify_code(db: AsyncSession, user: User) -> str:
    """Mint (and store) a fresh confirmation code; return it so the caller can
    e-mail it. Any previous code is replaced."""
    code = new_verify_code()
    user.email_verify_code = code
    user.email_verify_expires_at = utcnow() + timedelta(hours=settings.email_verify_ttl_hours)
    await db.flush()
    return code


async def confirm_email_code(db: AsyncSession, user: User, code: str) -> bool:
    """Check a typed code against the stored one; on success mark the address
    confirmed and clear the code."""
    stored = user.email_verify_code
    expires = user.email_verify_expires_at
    if not stored or expires is None or as_aware(expires) <= utcnow():
        return False
    if code.strip() != stored:
        return False
    user.email_verified_at = utcnow()
    user.email_verify_code = None
    user.email_verify_expires_at = None
    await db.flush()
    return True


async def get_user_by_login(db: AsyncSession, login: str) -> User | None:
    login = login.strip().lower()
    res = await db.execute(select(User).where((User.username == login) | (User.email == login)))
    return res.scalar_one_or_none()


async def mark_email_verified(db: AsyncSession, user: User) -> None:
    if user.email_verified_at is None:
        user.email_verified_at = utcnow()
        await db.flush()


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
