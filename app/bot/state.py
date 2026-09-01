"""Durable per-user bot state: the current domain and the last search."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BotUserState


async def get(db: AsyncSession, user_id: uuid.UUID) -> BotUserState | None:
    return await db.get(BotUserState, user_id)


async def _upsert(db: AsyncSession, user_id: uuid.UUID) -> BotUserState:
    st = await db.get(BotUserState, user_id)
    if st is None:
        st = BotUserState(user_id=user_id)
        db.add(st)
        await db.flush()
    return st


async def set_current_domain(
    db: AsyncSession, user_id: uuid.UUID, domain_id: uuid.UUID | None
) -> None:
    st = await _upsert(db, user_id)
    st.current_domain_id = domain_id
    await db.flush()


async def current_domain_id(db: AsyncSession, user_id: uuid.UUID) -> uuid.UUID | None:
    st = await db.get(BotUserState, user_id)
    return st.current_domain_id if st else None


async def set_last_search(db: AsyncSession, user_id: uuid.UUID, payload: dict) -> None:
    st = await _upsert(db, user_id)
    st.last_search = payload
    await db.flush()


async def last_search(db: AsyncSession, user_id: uuid.UUID) -> dict | None:
    st = await db.get(BotUserState, user_id)
    return st.last_search if st else None


async def clear_dangling_domain(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Drop the current domain if the user is no longer a member of it."""
    from app.models import DomainMember

    st = await db.get(BotUserState, user_id)
    if st is None or st.current_domain_id is None:
        return
    still = await db.scalar(
        select(DomainMember.user_id).where(
            DomainMember.domain_id == st.current_domain_id,
            DomainMember.user_id == user_id,
        )
    )
    if still is None:
        st.current_domain_id = None
        await db.flush()
