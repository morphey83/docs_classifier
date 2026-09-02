"""phase 8: email verification — user.email_verified_at

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-03
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user",
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Existing accounts predate verification — treat them as confirmed.
    op.execute('UPDATE "user" SET email_verified_at = CURRENT_TIMESTAMP')


def downgrade() -> None:
    op.drop_column("user", "email_verified_at")
