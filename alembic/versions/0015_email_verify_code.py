"""registration: confirm the address with a typed code instead of a link

Adds ``user.email_verify_code`` + ``user.email_verify_expires_at`` — the pending
six-digit code and its expiry. Only used when SMTP is configured.

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-03
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user", sa.Column("email_verify_code", sa.String(12), nullable=True))
    op.add_column(
        "user",
        sa.Column("email_verify_expires_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    with op.batch_alter_table("user") as batch_op:
        batch_op.drop_column("email_verify_expires_at")
        batch_op.drop_column("email_verify_code")
