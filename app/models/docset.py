"""Document sets — persistent, hand-curated collections of documents (§15)."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, uuid_pk


class SetVisibility(StrEnum):
    private = "private"  # only the creator
    domain = "domain"  # every member of the domain


_visibility_enum = Enum(SetVisibility, name="set_visibility", native_enum=False, length=16)


class DocumentSet(Base, TimestampMixin):
    __tablename__ = "document_set"

    id: Mapped[uuid.UUID] = uuid_pk()
    domain_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("domain.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    visibility: Mapped[SetVisibility] = mapped_column(
        _visibility_enum, default=SetVisibility.private, server_default="private"
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    item_count: Mapped[int] = mapped_column(Integer, default=0)


class DocumentSetItem(Base):
    __tablename__ = "document_set_item"

    set_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_set.id", ondelete="CASCADE"), primary_key=True
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document.id", ondelete="CASCADE"), primary_key=True
    )
    added_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    position: Mapped[int] = mapped_column(Integer, default=0)
