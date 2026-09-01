"""phase 6a: telegram account-link tokens

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tg_link_token",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("token", sa.String(64), nullable=False),
        sa.Column("tg_id", sa.BigInteger(), nullable=True),
        sa.Column("tg_username", sa.String(64), nullable=True),
        sa.Column("account_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["account_id"], ["user.id"], name="fk_tg_link_token_account_id_user", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_tg_link_token"),
    )
    op.create_index("ix_tg_link_token_token", "tg_link_token", ["token"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_tg_link_token_token", table_name="tg_link_token")
    op.drop_table("tg_link_token")
