"""Document-set, archive, and share-link schemas (§15)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.models import SetVisibility
from app.schemas.documents import DocumentOut


class SetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    visibility: SetVisibility = SetVisibility.private
    document_ids: list[uuid.UUID] | None = None


class SetUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    visibility: SetVisibility | None = None


class SetItemsAdd(BaseModel):
    document_ids: list[uuid.UUID] = Field(min_length=1)


class SetOut(BaseModel):
    id: uuid.UUID
    domain_id: uuid.UUID
    name: str
    description: str | None
    visibility: SetVisibility
    created_by: uuid.UUID | None
    item_count: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class SetItemOut(BaseModel):
    document: DocumentOut
    added_at: datetime
    position: int


class SetDetail(SetOut):
    items: list[SetItemOut] = []


class ArchiveStatusOut(BaseModel):
    status: str  # building | ready | failed
    ready: bool
    stale: bool  # a rebuild has been queued
    item_count: int
    missing_count: int
    size_bytes: int
    expires_at: datetime | None
    error: str | None = None


class LinkCreate(BaseModel):
    kind: Literal["permanent", "one_time"] = "one_time"
    expires_at: datetime | None = None


class LinkOut(BaseModel):
    id: uuid.UUID
    token: str
    url: str
    kind: str
    max_downloads: int | None
    download_count: int
    expires_at: datetime | None
    revoked_at: datetime | None
    last_downloaded_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}
