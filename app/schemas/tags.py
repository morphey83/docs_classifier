"""Tag schemas."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class TagUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    color: str | None = Field(default=None, max_length=16)


class TagMerge(BaseModel):
    into: uuid.UUID


class TagOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    color: str | None
    description: str | None
    created_at: datetime
    usage_count: int = 0

    model_config = {"from_attributes": True}


class TagOption(BaseModel):
    """A cross-domain tag-name option for filter pickers (GET /tags, §7)."""

    name: str
    usage_count: int
