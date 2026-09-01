"""phase 5: dedup guard exempts trashed rows

The ``(domain_id, sha256)`` uniqueness now applies only where
``deleted_at IS NULL``, so re-uploading content that is sitting in the trash
under a different name can still be ingested (docs/architecture.md §5).

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

_NAME = "uq_document_domain_id_sha256"
_WHERE = "deleted_at IS NULL"


def upgrade() -> None:
    with op.batch_alter_table("document") as batch_op:
        batch_op.drop_constraint(_NAME, type_="unique")
    op.create_index(
        _NAME,
        "document",
        ["domain_id", "sha256"],
        unique=True,
        sqlite_where=sa.text(_WHERE),
        postgresql_where=sa.text(_WHERE),
    )


def downgrade() -> None:
    op.drop_index(_NAME, table_name="document")
    with op.batch_alter_table("document") as batch_op:
        batch_op.create_unique_constraint(_NAME, ["domain_id", "sha256"])
