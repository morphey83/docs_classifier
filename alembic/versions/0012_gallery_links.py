"""§15: gallery share links + saved-filter dedup hash

- download_link.mode ("archive" | "gallery")
- document_set_filter.filter_hash (sha256 of the canonicalised filter)

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-03
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "download_link",
        sa.Column(
            "mode", sa.String(16), nullable=False, server_default="archive"
        ),
    )
    op.add_column(
        "document_set_filter", sa.Column("filter_hash", sa.String(64), nullable=True)
    )
    op.create_index(
        "ix_document_set_filter_filter_hash", "document_set_filter", ["filter_hash"]
    )


def downgrade() -> None:
    op.drop_index("ix_document_set_filter_filter_hash", table_name="document_set_filter")
    op.drop_column("document_set_filter", "filter_hash")
    op.drop_column("download_link", "mode")
