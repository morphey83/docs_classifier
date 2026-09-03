"""Tag schemas."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class TagUpdate(BaseModel):
    # names are fixed; only the caller's own colour is editable ("" clears it)
    color: str | None = Field(default=None, max_length=16)


class TagMerge(BaseModel):
    into: uuid.UUID


class TagOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    color: str | None = None  # the requesting user's own colour, if any
    description: str | None = None
    created_at: datetime
    usage_count: int = 0

    model_config = {"from_attributes": True}


class TagOption(BaseModel):
    """A cross-domain tag-name option for filter pickers (GET /tags, §7)."""

    name: str
    usage_count: int
