"""domains, documents, tags

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.models.base import JSONVariant

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

_ROLE = sa.Enum(
    "owner", "admin", "editor", "tagger", "viewer", "scanner",
    name="role", native_enum=False, length=16,
)


def upgrade() -> None:
    op.create_table(
        "domain",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("description", sa.String(2000), nullable=True),
        sa.Column("settings", JSONVariant, server_default="{}", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["owner_id"], ["user.id"], name="fk_domain_owner_id_user", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="pk_domain"),
    )
    op.create_index("ix_domain_slug", "domain", ["slug"], unique=True)

    op.create_table(
        "domain_member",
        sa.Column("domain_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", _ROLE, nullable=False),
        sa.Column("added_by", sa.Uuid(), nullable=True),
        sa.Column("added_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["added_by"], ["user.id"], name="fk_domain_member_added_by_user", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["domain_id"], ["domain.id"], name="fk_domain_member_domain_id_domain", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], name="fk_domain_member_user_id_user", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("domain_id", "user_id", name="pk_domain_member"),
    )

    op.create_table(
        "domain_invite",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("domain_id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(320), nullable=True),
        sa.Column("username", sa.String(64), nullable=True),
        sa.Column("role", _ROLE, nullable=False),
        sa.Column("token", sa.String(64), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["accepted_by"], ["user.id"], name="fk_domain_invite_accepted_by_user", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by"], ["user.id"], name="fk_domain_invite_created_by_user", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["domain_id"], ["domain.id"], name="fk_domain_invite_domain_id_domain", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_domain_invite"),
        sa.UniqueConstraint("domain_id", "email", name="uq_domain_invite_domain_id_email"),
    )
    op.create_index("ix_domain_invite_domain_id", "domain_invite", ["domain_id"])
    op.create_index("ix_domain_invite_token", "domain_invite", ["token"], unique=True)

    op.create_table(
        "tag",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("domain_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("color", sa.String(16), nullable=True),
        sa.Column("description", sa.String(500), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["user.id"], name="fk_tag_created_by_user", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["domain_id"], ["domain.id"], name="fk_tag_domain_id_domain", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_tag"),
        sa.UniqueConstraint("domain_id", "slug", name="uq_tag_domain_id_slug"),
    )
    op.create_index("ix_tag_domain_id", "tag", ["domain_id"])

    op.create_table(
        "upload_batch",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("domain_id", sa.Uuid(), nullable=False),
        sa.Column("uploaded_by", sa.Uuid(), nullable=False),
        sa.Column("source_filename", sa.String(500), nullable=False),
        sa.Column("kind", sa.Enum("single", "archive", name="batch_kind", native_enum=False, length=16), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("conflict_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["domain_id"], ["domain.id"], name="fk_upload_batch_domain_id_domain", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["uploaded_by"], ["user.id"], name="fk_upload_batch_uploaded_by_user", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_upload_batch"),
    )
    op.create_index("ix_upload_batch_domain_id", "upload_batch", ["domain_id"])

    op.create_table(
        "document",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("domain_id", sa.Uuid(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("storage_key", sa.String(80), nullable=False),
        sa.Column("original_name", sa.String(500), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("mime", sa.String(160), nullable=False),
        sa.Column("ext", sa.String(32), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("doc_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("status", sa.Enum("inbox", "tagged", "archived", name="doc_status", native_enum=False, length=16), nullable=False),
        sa.Column("source", sa.Enum("upload", "archive", "bot", name="doc_source", native_enum=False, length=16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("upload_batch_id", sa.Uuid(), nullable=True),
        sa.Column("uploaded_by", sa.Uuid(), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(["deleted_by"], ["user.id"], name="fk_document_deleted_by_user", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["domain_id"], ["domain.id"], name="fk_document_domain_id_domain", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["upload_batch_id"], ["upload_batch.id"], name="fk_document_upload_batch_id_upload_batch", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["uploaded_by"], ["user.id"], name="fk_document_uploaded_by_user", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_document"),
        sa.UniqueConstraint("domain_id", "sha256", name="uq_document_domain_id_sha256"),
    )
    op.create_index("ix_document_domain_id", "document", ["domain_id"])
    op.create_index("ix_document_sha256", "document", ["sha256"])
    op.create_index("ix_document_status", "document", ["status"])
    op.create_index("ix_document_doc_date", "document", ["doc_date"])
    op.create_index("ix_document_deleted_at", "document", ["deleted_at"])

    op.create_table(
        "document_tag",
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("tag_id", sa.Uuid(), nullable=False),
        sa.Column("assigned_by", sa.Uuid(), nullable=True),
        sa.Column("assigned_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["assigned_by"], ["user.id"], name="fk_document_tag_assigned_by_user", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["document_id"], ["document.id"], name="fk_document_tag_document_id_document", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tag_id"], ["tag.id"], name="fk_document_tag_tag_id_tag", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("document_id", "tag_id", name="pk_document_tag"),
    )

    op.create_table(
        "document_version",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("storage_key", sa.String(80), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("doc_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("original_name", sa.String(500), nullable=False),
        sa.Column("replaced_by", sa.Uuid(), nullable=True),
        sa.Column("replaced_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["document.id"], name="fk_document_version_document_id_document", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["replaced_by"], ["user.id"], name="fk_document_version_replaced_by_user", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_document_version"),
    )
    op.create_index("ix_document_version_document_id", "document_version", ["document_id"])

    op.create_table(
        "inbox_defer",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("deferred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["document.id"], name="fk_inbox_defer_document_id_document", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], name="fk_inbox_defer_user_id_user", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "document_id", name="pk_inbox_defer"),
    )


def downgrade() -> None:
    for table in (
        "inbox_defer",
        "document_version",
        "document_tag",
        "document",
        "upload_batch",
        "tag",
        "domain_invite",
        "domain_member",
        "domain",
    ):
        op.drop_table(table)
