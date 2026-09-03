"""§15: document_set.description → TEXT (large free-form text)

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-03
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("document_set") as batch_op:
        batch_op.alter_column(
            "description",
            existing_type=sa.String(2000),
            type_=sa.Text(),
            existing_nullable=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("document_set") as batch_op:
        batch_op.alter_column(
            "description",
            existing_type=sa.Text(),
            type_=sa.String(2000),
            existing_nullable=True,
        )
