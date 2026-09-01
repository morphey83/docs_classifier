"""Upload-batch schemas."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel


class BatchItemOut(BaseModel):
    entry_name: str
    outcome: str
    document_id: uuid.UUID | None
    note: str | None

    model_config = {"from_attributes": True}


class BatchOut(BaseModel):
    id: uuid.UUID
    domain_id: uuid.UUID
    source_filename: str
    kind: str
    status: str
    item_count: int
    conflict_count: int
    error: str | None
    uploaded_at: datetime

    model_config = {"from_attributes": True}


class BatchDetail(BatchOut):
    items: list[BatchItemOut] = []
