"""phase 6b: per-user bot state

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.models.base import JSONVariant

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bot_user_state",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("current_domain_id", sa.Uuid(), nullable=True),
        sa.Column("last_search", JSONVariant, nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["current_domain_id"], ["domain.id"], name="fk_bot_user_state_current_domain_id_domain", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], name="fk_bot_user_state_user_id_user", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", name="pk_bot_user_state"),
    )


def downgrade() -> None:
    op.drop_table("bot_user_state")
