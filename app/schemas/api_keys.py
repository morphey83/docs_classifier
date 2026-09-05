"""Per-device API key schemas (Bearer auth for native/mobile clients)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class ApiKeyOut(BaseModel):
    id: uuid.UUID
    name: str
    created_at: datetime
    last_used_at: datetime | None
    # only set once, in the create response — never recoverable afterwards
    token: str | None = None

    model_config = {"from_attributes": True}
