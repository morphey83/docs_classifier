"""§15 rev 4: sets belong to a user, defined as saved filters + explicit adds

- document_set loses domain_id / visibility / item_count, gains owner_id
- new document_set_filter (a saved SearchFilters per set)
- document gains is_public (share-link visibility gate)
- artifact.domain_id becomes nullable (a set archive has no domain)

Existing sets are test-phase data and are dropped.

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-03
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.models.base import JSONVariant

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

_VIS = sa.Enum("private", "domain", name="set_visibility", native_enum=False, length=16)


def upgrade() -> None:
    # --- document.is_public -------------------------------------------------
    op.add_column(
        "document",
        sa.Column(
            "is_public", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )

    # --- artifact.domain_id -> nullable -----------------------------------
    with op.batch_alter_table("artifact") as batch_op:
        batch_op.alter_column("domain_id", existing_type=sa.Uuid(), nullable=True)

    # --- rebuild the set tables ------------------------------------------
    op.drop_table("document_set_item")
    op.drop_index("ix_document_set_domain_id", table_name="document_set")
    op.drop_table("document_set")

    op.create_table(
        "document_set",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.String(2000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["user.id"], name="fk_document_set_owner_id_user", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_document_set"),
    )
    op.create_index("ix_document_set_owner_id", "document_set", ["owner_id"])

    op.create_table(
        "document_set_filter",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("set_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("filter", JSONVariant, server_default="{}", nullable=False),
        sa.Column("description", sa.String(500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["set_id"], ["document_set.id"],
            name="fk_document_set_filter_set_id_document_set", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_document_set_filter"),
    )
    op.create_index("ix_document_set_filter_set_id", "document_set_filter", ["set_id"])

    op.create_table(
        "document_set_item",
        sa.Column("set_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("added_by", sa.Uuid(), nullable=True),
        sa.Column("added_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["added_by"], ["user.id"],
            name="fk_document_set_item_added_by_user", ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"], ["document.id"],
            name="fk_document_set_item_document_id_document", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["set_id"], ["document_set.id"],
            name="fk_document_set_item_set_id_document_set", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("set_id", "document_id", name="pk_document_set_item"),
    )


def downgrade() -> None:
    op.drop_table("document_set_item")
    op.drop_index("ix_document_set_filter_set_id", table_name="document_set_filter")
    op.drop_table("document_set_filter")
    op.drop_index("ix_document_set_owner_id", table_name="document_set")
    op.drop_table("document_set")

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

    with op.batch_alter_table("artifact") as batch_op:
        batch_op.alter_column("domain_id", existing_type=sa.Uuid(), nullable=False)
    op.drop_column("document", "is_public")
