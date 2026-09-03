"""§7: tag colour becomes a per-user preference

``tag.color`` (one global colour) → ``user_tag_color (user_id, tag_id, color)``.
A new user sees no colours and can never change another user's view. Existing
global colours are test-phase data and are dropped.

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-03
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_tag_color",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("tag_id", sa.Uuid(), nullable=False),
        sa.Column("color", sa.String(16), nullable=False),
        sa.ForeignKeyConstraint(
            ["tag_id"], ["tag.id"], name="fk_user_tag_color_tag_id_tag", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["user.id"], name="fk_user_tag_color_user_id_user", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("user_id", "tag_id", name="pk_user_tag_color"),
    )
    with op.batch_alter_table("tag") as batch_op:
        batch_op.drop_column("color")


def downgrade() -> None:
    with op.batch_alter_table("tag") as batch_op:
        batch_op.add_column(sa.Column("color", sa.String(16), nullable=True))
    op.drop_table("user_tag_color")
