"""Helpers shared by API routers."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Document
from app.schemas.documents import DocumentOut


async def document_out(db: AsyncSession, doc: Document) -> DocumentOut:
    """Serialise a Document, making sure its ``tags`` relationship is loaded."""
    await db.refresh(doc, attribute_names=["tags"])
    return DocumentOut.model_validate(doc)
