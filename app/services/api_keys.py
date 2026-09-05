"""Per-device API keys for native/mobile clients (Bearer auth). The raw token
is shown to the caller exactly once, at creation — only its hash is ever
stored, so a lost token can only be revoked, never recovered."""

from __future__ import annotations

import hashlib
import secrets
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ApiKey, User
from app.util.time import utcnow


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


async def create_api_key(db: AsyncSession, user: User, *, name: str) -> tuple[ApiKey, str]:
    raw = "dc_" + secrets.token_urlsafe(32)
    key = ApiKey(user_id=user.id, name=(name.strip() or "Устройство")[:120], key_hash=_hash(raw))
    db.add(key)
    await db.flush()
    return key, raw


async def list_api_keys(db: AsyncSession, user_id: uuid.UUID) -> list[ApiKey]:
    rows = await db.scalars(
        select(ApiKey)
        .where(ApiKey.user_id == user_id, ApiKey.revoked_at.is_(None))
        .order_by(ApiKey.created_at.desc())
    )
    return list(rows)


async def get_owned_key(db: AsyncSession, key_id: uuid.UUID, user_id: uuid.UUID) -> ApiKey | None:
    key = await db.get(ApiKey, key_id)
    return key if key is not None and key.user_id == user_id else None


async def revoke_api_key(db: AsyncSession, key: ApiKey) -> None:
    if key.revoked_at is None:
        key.revoked_at = utcnow()
        await db.flush()


async def resolve_api_key(db: AsyncSession, raw: str) -> User | None:
    """Bearer-token lookup: hash, exact match, must be unrevoked and active."""
    if not raw:
        return None
    key = await db.scalar(select(ApiKey).where(ApiKey.key_hash == _hash(raw)))
    if key is None or key.revoked_at is not None:
        return None
    user = await db.get(User, key.user_id)
    if user is None or not user.is_active:
        return None
    key.last_used_at = utcnow()
    await db.flush()
    return user
