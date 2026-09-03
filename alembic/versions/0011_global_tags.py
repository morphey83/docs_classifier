"""§7 rev 2: one global tag pool — tags are no longer owned by a domain

`tag` loses `domain_id` (+ its FK/index and the `(domain_id, slug)` unique);
`slug` becomes globally unique. Existing tags/assignments are test-phase data
and are cleared.

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-03
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DELETE FROM document_tag")
    op.execute("DELETE FROM tag")
    with op.batch_alter_table("tag") as batch_op:
        batch_op.drop_constraint("uq_tag_domain_id_slug", type_="unique")
        batch_op.drop_index("ix_tag_domain_id")
        batch_op.drop_column("domain_id")
        batch_op.create_unique_constraint("uq_tag_slug", ["slug"])


def downgrade() -> None:
    op.execute("DELETE FROM document_tag")
    op.execute("DELETE FROM tag")
    with op.batch_alter_table("tag") as batch_op:
        batch_op.drop_constraint("uq_tag_slug", type_="unique")
        batch_op.add_column(sa.Column("domain_id", sa.Uuid(), nullable=False))
        batch_op.create_foreign_key(
            "fk_tag_domain_id_domain", "domain", ["domain_id"], ["id"], ondelete="CASCADE"
        )
        batch_op.create_index("ix_tag_domain_id", ["domain_id"])
        batch_op.create_unique_constraint("uq_tag_domain_id_slug", ["domain_id", "slug"])
