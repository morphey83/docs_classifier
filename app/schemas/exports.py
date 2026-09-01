"""Export / artifact schemas."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel


class ExportCreate(BaseModel):
    document_ids: list[uuid.UUID] | None = None
    # if document_ids is omitted, the current search filter is used:
    q: str | None = None
    status: str | None = None
    tags_all: list[str] | None = None
    tags_any: list[str] | None = None
    tags_none: list[str] | None = None
    ext: str | None = None
    mime: str | None = None


class ArtifactOut(BaseModel):
    id: uuid.UUID
    domain_id: uuid.UUID
    kind: str
    status: str
    size_bytes: int
    item_count: int
    missing_count: int
    error: str | None
    created_at: datetime
    expires_at: datetime | None

    model_config = {"from_attributes": True}
