"""Document sets — user-owned collections defined as saved filters + explicit
adds, resolved live (§15 rev 4)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, JSONVariant, TimestampMixin, uuid_pk


class DocumentSet(Base, TimestampMixin):
    __tablename__ = "document_set"

    id: Mapped[uuid.UUID] = uuid_pk()
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)


class DocumentSetFilter(Base):
    """A saved search attached to a set. Its ``filter`` is a serialized
    ``SearchFilters`` (see ``app.services.search``), including a ``domain_ids``
    list — empty means every domain the owner can currently reach."""

    __tablename__ = "document_set_filter"

    id: Mapped[uuid.UUID] = uuid_pk()
    set_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_set.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer, default=0)
    filter: Mapped[dict] = mapped_column(JSONVariant, default=dict, server_default="{}")
    # sha256 of the canonicalised filter — dedupes "add the same filter again"
    filter_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    description: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DocumentSetItem(Base):
    """An explicitly-added document. Always in the set's result while the owner
    keeps ``download`` on it. There is no exclusion counterpart — to drop a
    filter match, narrow the filter."""

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
