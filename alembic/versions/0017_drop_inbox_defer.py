"""§14: drop deferring — the tagging walkthrough now just "skips" in-session

No more persisted per-user "defer" state. The card-by-card modal's "Пропустить"
button skips forward for this pass only (an in-request exclude list), instead
of writing a row that used to hide the document from the inbox preset forever.

Revision ID: 0017
Revises: 0016
Create Date: 2026-09-05
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("inbox_defer")


def downgrade() -> None:
    op.create_table(
        "inbox_defer",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column(
            "deferred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["document_id"], ["document.id"], name="fk_inbox_defer_document_id_document",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["user.id"], name="fk_inbox_defer_user_id_user", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("user_id", "document_id", name="pk_inbox_defer"),
    )
