"""phase 2: batch items, document text/index, artifacts, FTS

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.models.base import JSONVariant

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

_TEXT_SRC = sa.Enum("none", "parsed", "ocr", name="text_source", native_enum=False, length=16)
_IDX_ST = sa.Enum("none", "pending", "done", "failed", name="index_status", native_enum=False, length=16)


def upgrade() -> None:
    op.create_table(
        "upload_batch_item",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("entry_name", sa.String(1000), nullable=False),
        sa.Column("outcome", sa.String(24), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["batch_id"], ["upload_batch.id"], name="fk_upload_batch_item_batch_id_upload_batch", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["document.id"], name="fk_upload_batch_item_document_id_document", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_upload_batch_item"),
    )
    op.create_index("ix_upload_batch_item_batch_id", "upload_batch_item", ["batch_id"])

    op.add_column("document", sa.Column("extracted_text", sa.Text(), nullable=True))
    op.add_column("document", sa.Column("text_source", _TEXT_SRC, nullable=False, server_default="none"))
    op.add_column("document", sa.Column("index_status", _IDX_ST, nullable=False, server_default="none"))
    op.add_column("document", sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_document_indexed_at", "document", ["indexed_at"])

    op.create_table(
        "artifact",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("domain_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Enum("adhoc_export", "set_archive", name="artifact_kind", native_enum=False, length=20), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.Enum("building", "ready", "failed", name="artifact_status", native_enum=False, length=12), nullable=False),
        sa.Column("storage_key", sa.String(120), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("missing_count", sa.Integer(), nullable=False),
        sa.Column("snapshot", JSONVariant, server_default="{}", nullable=False),
        sa.Column("error", sa.String(2000), nullable=True),
        sa.Column("requested_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["domain_id"], ["domain.id"], name="fk_artifact_domain_id_domain", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["requested_by"], ["user.id"], name="fk_artifact_requested_by_user", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_artifact"),
    )
    op.create_index("ix_artifact_domain_id", "artifact", ["domain_id"])
    op.create_index("ix_artifact_expires_at", "artifact", ["expires_at"])

    op.create_table(
        "download_link",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("artifact_id", sa.Uuid(), nullable=False),
        sa.Column("token", sa.String(64), nullable=False),
        sa.Column("max_downloads", sa.Integer(), nullable=True),
        sa.Column("download_count", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_downloaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["artifact_id"], ["artifact.id"], name="fk_download_link_artifact_id_artifact", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["user.id"], name="fk_download_link_created_by_user", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_download_link"),
    )
    op.create_index("ix_download_link_artifact_id", "download_link", ["artifact_id"])
    op.create_index("ix_download_link_token", "download_link", ["token"], unique=True)

    # --- full-text search: PostgreSQL only, invisible to the ORM / alembic check
    if op.get_bind().dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        op.execute("ALTER TABLE document ADD COLUMN search_tsv tsvector")
        op.execute("CREATE INDEX ix_document_search_fts ON document USING gin (search_tsv)")
        op.execute("CREATE INDEX ix_document_title_trgm ON document USING gin (title gin_trgm_ops)")
        op.execute("CREATE INDEX ix_tag_name_trgm ON tag USING gin (name gin_trgm_ops)")


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for stmt in (
            "DROP INDEX IF EXISTS ix_tag_name_trgm",
            "DROP INDEX IF EXISTS ix_document_title_trgm",
            "DROP INDEX IF EXISTS ix_document_search_fts",
            "ALTER TABLE document DROP COLUMN IF EXISTS search_tsv",
        ):
            op.execute(stmt)

    op.drop_index("ix_download_link_token", table_name="download_link")
    op.drop_index("ix_download_link_artifact_id", table_name="download_link")
    op.drop_table("download_link")
    op.drop_index("ix_artifact_expires_at", table_name="artifact")
    op.drop_index("ix_artifact_domain_id", table_name="artifact")
    op.drop_table("artifact")
    op.drop_index("ix_document_indexed_at", table_name="document")
    op.drop_column("document", "indexed_at")
    op.drop_column("document", "index_status")
    op.drop_column("document", "text_source")
    op.drop_column("document", "extracted_text")
    op.drop_index("ix_upload_batch_item_batch_id", table_name="upload_batch_item")
    op.drop_table("upload_batch_item")
