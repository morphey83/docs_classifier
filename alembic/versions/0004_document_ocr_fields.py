"""phase 3: document OCR fields

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

_OCR = sa.Enum(
    "none", "pending", "done", "failed", "unsupported",
    name="ocr_status", native_enum=False, length=16,
)


def upgrade() -> None:
    op.add_column(
        "document",
        sa.Column("ocr_status", _OCR, nullable=False, server_default="none"),
    )
    op.add_column("document", sa.Column("ocr_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("document", sa.Column("ocr_lang", sa.String(32), nullable=True))
    op.create_index("ix_document_ocr_at", "document", ["ocr_at"])


def downgrade() -> None:
    op.drop_index("ix_document_ocr_at", table_name="document")
    op.drop_column("document", "ocr_lang")
    op.drop_column("document", "ocr_at")
    op.drop_column("document", "ocr_status")
