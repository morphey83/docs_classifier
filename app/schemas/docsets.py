"""Document-set, archive, and share-link schemas (§15 rev 4)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class SetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    document_ids: list[uuid.UUID] | None = None


class SetUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)


class SetItemsAdd(BaseModel):
    document_ids: list[uuid.UUID] = Field(min_length=1)


class FilterAdd(BaseModel):
    filter: dict = Field(default_factory=dict)
    description: str = Field(default="", max_length=500)


class FilterOut(BaseModel):
    id: uuid.UUID
    position: int
    filter: dict
    description: str
    created_at: datetime

    model_config = {"from_attributes": True}


class SetOut(BaseModel):
    id: uuid.UUID
    owner_id: uuid.UUID
    name: str
    description: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class SetDetail(SetOut):
    filters: list[FilterOut] = []
    item_count: int = 0
    resolved_count: int = 0


class ArchiveStatusOut(BaseModel):
    status: str  # building | ready | failed
    ready: bool
    stale: bool
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
