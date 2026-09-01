"""Per-user bot state — current domain and last search, persisted (§8)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, JSONVariant


class BotUserState(Base):
    __tablename__ = "bot_user_state"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), primary_key=True
    )
    current_domain_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("domain.id", ondelete="SET NULL"), nullable=True
    )
    last_search: Mapped[dict | None] = mapped_column(JSONVariant, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
