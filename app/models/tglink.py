"""Bidirectional Telegram <-> account linking tokens (§8)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, uuid_pk


class TgLinkToken(Base):
    """Bridges a Telegram id and a service account.

    Created with exactly one side known — ``tg_id`` (bot-initiated: `/start`
    with no payload) or ``account_id`` (web-initiated: profile page) — and
    consumed once the other side confirms. Single-use, short TTL.
    """

    __tablename__ = "tg_link_token"

    id: Mapped[uuid.UUID] = uuid_pk()
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    tg_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    tg_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
