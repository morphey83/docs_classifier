"""Built export archives (artifacts) and their public download links.

Phase 2 uses only ``Artifact`` + authed download. ``DownloadLink`` and the
public ``GET /d/{token}`` route arrive in Phase 4 (document sets & sharing).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, JSONVariant, uuid_pk


class ArtifactKind(StrEnum):
    adhoc_export = "adhoc_export"
    set_archive = "set_archive"


class ArtifactStatus(StrEnum):
    building = "building"
    ready = "ready"
    failed = "failed"


_kind_enum = Enum(ArtifactKind, name="artifact_kind", native_enum=False, length=20)
_status_enum = Enum(ArtifactStatus, name="artifact_status", native_enum=False, length=12)


class Artifact(Base):
    __tablename__ = "artifact"

    id: Mapped[uuid.UUID] = uuid_pk()
    domain_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("domain.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[ArtifactKind] = mapped_column(_kind_enum)
    source_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)  # set_id, for set_archive
    status: Mapped[ArtifactStatus] = mapped_column(_status_enum, default=ArtifactStatus.building)
    storage_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    item_count: Mapped[int] = mapped_column(Integer, default=0)
    missing_count: Mapped[int] = mapped_column(Integer, default=0)
    snapshot: Mapped[dict] = mapped_column(JSONVariant, default=dict, server_default="{}")
    error: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )


class DownloadLink(Base):
    __tablename__ = "download_link"

    id: Mapped[uuid.UUID] = uuid_pk()
    artifact_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("artifact.id", ondelete="CASCADE"), index=True
    )
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    max_downloads: Mapped[int | None] = mapped_column(Integer, nullable=True)
    download_count: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_downloaded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
