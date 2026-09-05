"""Document, tag, upload-batch, version, and inbox-defer models."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    false,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, uuid_pk
from app.util.time import utcnow


class DocStatus(StrEnum):
    inbox = "inbox"
    tagged = "tagged"
    archived = "archived"


class DocSource(StrEnum):
    upload = "upload"
    archive = "archive"
    bot = "bot"


class BatchKind(StrEnum):
    single = "single"
    archive = "archive"


class TextSource(StrEnum):
    none = "none"
    parsed = "parsed"
    ocr = "ocr"


class IndexStatus(StrEnum):
    none = "none"
    pending = "pending"
    done = "done"
    failed = "failed"


class OcrStatus(StrEnum):
    none = "none"
    pending = "pending"
    done = "done"
    failed = "failed"
    unsupported = "unsupported"


_status_enum = Enum(DocStatus, name="doc_status", native_enum=False, length=16)
_source_enum = Enum(DocSource, name="doc_source", native_enum=False, length=16)
_batch_kind_enum = Enum(BatchKind, name="batch_kind", native_enum=False, length=16)
_text_source_enum = Enum(TextSource, name="text_source", native_enum=False, length=16)
_index_status_enum = Enum(IndexStatus, name="index_status", native_enum=False, length=16)
_ocr_status_enum = Enum(OcrStatus, name="ocr_status", native_enum=False, length=16)


class UploadBatch(Base):
    __tablename__ = "upload_batch"

    id: Mapped[uuid.UUID] = uuid_pk()
    domain_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("domain.id", ondelete="CASCADE"), index=True
    )
    uploaded_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))
    source_filename: Mapped[str] = mapped_column(String(500))
    kind: Mapped[BatchKind] = mapped_column(_batch_kind_enum)
    item_count: Mapped[int] = mapped_column(Integer, default=0)
    conflict_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="processing")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    items: Mapped[list[UploadBatchItem]] = relationship(
        back_populates="batch", cascade="all, delete-orphan"
    )


class UploadBatchItem(Base):
    __tablename__ = "upload_batch_item"

    id: Mapped[uuid.UUID] = uuid_pk()
    batch_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("upload_batch.id", ondelete="CASCADE"), index=True
    )
    entry_name: Mapped[str] = mapped_column(String(1000))
    outcome: Mapped[str] = mapped_column(String(24))  # created / deduplicated / … / error / skipped
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("document.id", ondelete="SET NULL"), nullable=True
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    batch: Mapped[UploadBatch] = relationship(back_populates="items")


class Document(Base):
    __tablename__ = "document"

    id: Mapped[uuid.UUID] = uuid_pk()
    domain_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("domain.id", ondelete="CASCADE"), index=True
    )
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    storage_key: Mapped[str] = mapped_column(String(80))
    original_name: Mapped[str] = mapped_column(String(500))
    title: Mapped[str] = mapped_column(String(500))
    mime: Mapped[str] = mapped_column(String(160), default="application/octet-stream")
    ext: Mapped[str] = mapped_column(String(32), default="")
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    doc_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[DocStatus] = mapped_column(_status_enum, default=DocStatus.inbox, index=True)
    source: Mapped[DocSource] = mapped_column(_source_enum, default=DocSource.upload)
    version: Mapped[int] = mapped_column(Integer, default=1)

    # May this document leave its domain through a set's share link? Set at
    # ingest from domain.settings.default_document_visibility; changed by a
    # `manage` holder per-doc or in bulk (§15). Pure sharing gate — in-domain
    # visibility stays RBAC.
    is_public: Mapped[bool] = mapped_column(default=False, server_default=false())

    # Can be large (a whole document's body). Never read back for display —
    # only written during indexing and matched via SQL — so keep it off the
    # default SELECT; it loads lazily if something ever touches the attribute.
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True, deferred=True)
    text_source: Mapped[TextSource] = mapped_column(
        _text_source_enum, default=TextSource.none, server_default="none"
    )
    index_status: Mapped[IndexStatus] = mapped_column(
        _index_status_enum, default=IndexStatus.none, server_default="none"
    )
    indexed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    ocr_status: Mapped[OcrStatus] = mapped_column(
        _ocr_status_enum, default=OcrStatus.none, server_default="none"
    )
    ocr_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    ocr_lang: Mapped[str | None] = mapped_column(String(32), nullable=True)

    upload_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("upload_batch.id", ondelete="SET NULL"), nullable=True
    )
    uploaded_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))
    # Python-side default too: sub-second precision keeps the inbox FIFO order
    # stable (SQLite's CURRENT_TIMESTAMP only has 1-second resolution).
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now()
    )

    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    deleted_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )

    tags: Mapped[list[Tag]] = relationship(
        secondary="document_tag", lazy="selectin", order_by="Tag.name"
    )

    # Dedup guard: at most one *active* document per (domain, content). Trashed
    # rows are exempt so a re-upload of trashed content can still be ingested
    # (docs/architecture.md §5).
    __table_args__ = (
        Index(
            "uq_document_domain_id_sha256",
            "domain_id",
            "sha256",
            unique=True,
            sqlite_where=text("deleted_at IS NULL"),
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )


class Tag(Base, TimestampMixin):
    """A global tag. Not owned by a domain — one shared pool (§7 rev 2). A tag
    lives while at least one document carries it; the nightly cleanup sweeps
    tags that drop to zero references."""

    __tablename__ = "tag"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(120))
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )


class DocumentTag(Base):
    __tablename__ = "document_tag"

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document.id", ondelete="CASCADE"), primary_key=True
    )
    tag_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tag.id", ondelete="CASCADE"), primary_key=True
    )
    assigned_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class UserTagColor(Base):
    """A tag's colour is a per-user preference (§7): a new user sees no
    colours, and one person's choice never changes another's view."""

    __tablename__ = "user_tag_color"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), primary_key=True
    )
    tag_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tag.id", ondelete="CASCADE"), primary_key=True
    )
    color: Mapped[str] = mapped_column(String(16))


class DocumentVersion(Base):
    __tablename__ = "document_version"

    id: Mapped[uuid.UUID] = uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document.id", ondelete="CASCADE"), index=True
    )
    version_no: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    storage_key: Mapped[str] = mapped_column(String(80))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    doc_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    original_name: Mapped[str] = mapped_column(String(500))
    replaced_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    replaced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
