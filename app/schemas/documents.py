"""Document schemas."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models import DocSource, DocStatus, IndexStatus, TextSource
from app.schemas.tags import TagOut


class DocumentOut(BaseModel):
    id: uuid.UUID
    domain_id: uuid.UUID
    sha256: str
    original_name: str
    title: str
    mime: str
    ext: str
    size_bytes: int
    doc_date: datetime | None
    notes: str | None
    status: DocStatus
    source: DocSource
    version: int
    text_source: TextSource
    index_status: IndexStatus
    indexed_at: datetime | None
    uploaded_at: datetime
    uploaded_by: uuid.UUID
    deleted_at: datetime | None
    tags: list[TagOut] = []

    model_config = {"from_attributes": True}


class DocumentUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    doc_date: datetime | None = None
    clear_doc_date: bool = False
    notes: str | None = Field(default=None, max_length=20000)


class DocumentTagsUpdate(BaseModel):
    tag_ids: list[uuid.UUID] | None = None
    tag_names: list[str] | None = None


class UploadResult(BaseModel):
    outcome: str
    document: DocumentOut


class FacetBucket(BaseModel):
    count: int


class Facets(BaseModel):
    tags: list[dict]
    types: list[dict]
    status: dict[str, int]


class DocumentList(BaseModel):
    items: list[DocumentOut]
    total: int
    page: int
    page_size: int
    facets: Facets | None = None


class InboxStatus(BaseModel):
    count: int
