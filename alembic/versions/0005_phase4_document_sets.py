"""phase 4: document sets + set-archive content hash

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

_VIS = sa.Enum("private", "domain", name="set_visibility", native_enum=False, length=16)


def upgrade() -> None:
    op.add_column("artifact", sa.Column("content_hash", sa.String(64), nullable=True))

    op.create_table(
        "document_set",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("domain_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.String(2000), nullable=True),
        sa.Column("visibility", _VIS, server_default="private", nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["user.id"], name="fk_document_set_created_by_user", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["domain_id"], ["domain.id"], name="fk_document_set_domain_id_domain", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_document_set"),
    )
    op.create_index("ix_document_set_domain_id", "document_set", ["domain_id"])

    op.create_table(
        "document_set_item",
        sa.Column("set_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("added_by", sa.Uuid(), nullable=True),
        sa.Column("added_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["added_by"], ["user.id"], name="fk_document_set_item_added_by_user", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["document_id"], ["document.id"], name="fk_document_set_item_document_id_document", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["set_id"], ["document_set.id"], name="fk_document_set_item_set_id_document_set", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("set_id", "document_id", name="pk_document_set_item"),
    )


def downgrade() -> None:
    op.drop_table("document_set_item")
    op.drop_index("ix_document_set_domain_id", table_name="document_set")
    op.drop_table("document_set")
    op.drop_column("artifact", "content_hash")
