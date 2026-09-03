"""§7/§14: «не размечено» = «нет тегов» — realign document.status with reality

`Document.status` (inbox|tagged) is now a cache of "has ≥1 tag", kept in step by
``tags.set_document_tags``. Fix rows that drifted (tagged via the doc card without
leaving the inbox, or emptied without returning to it). ``archived`` is untouched.

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-03
"""

from __future__ import annotations

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE document SET status = 'tagged' "
        "WHERE status = 'inbox' "
        "AND EXISTS (SELECT 1 FROM document_tag WHERE document_id = document.id)"
    )
    op.execute(
        "UPDATE document SET status = 'inbox' "
        "WHERE status = 'tagged' "
        "AND NOT EXISTS (SELECT 1 FROM document_tag WHERE document_id = document.id)"
    )


def downgrade() -> None:
    # data-only realignment; nothing to undo
    pass
