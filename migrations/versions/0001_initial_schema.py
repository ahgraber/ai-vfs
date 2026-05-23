"""initial schema

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-05-22

Creates the Phase 1 metadata schema (files, versions, permissions, audit, names,
namespaces, principals) shared by the SQLite and PostgreSQL adapters. JSON columns use
the portable variant from ``vfs.stores.schema`` so they render as JSONB on PostgreSQL
and TEXT-backed JSON on SQLite. Booleans are stored as integer flags and timestamps as
ISO-8601 text, identically across both dialects.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Type definitions are frozen inside the revision (not imported from vfs.stores.schema)
# so replaying this migration always reproduces the historical schema even if the
# application schema helpers change later.
def _json() -> sa.types.TypeEngine:
    return sa.JSON().with_variant(JSONB(), "postgresql")


def _bool() -> sa.types.TypeEngine:
    return sa.Boolean(create_constraint=False)


def upgrade() -> None:
    op.create_table(
        "namespaces",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("display_name", sa.Text, nullable=False),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.Column("created_by", sa.Text, nullable=False),
        sa.Column("retention_policy", sa.Text, nullable=True),
    )
    op.create_table(
        "principals",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("display_name", sa.Text, nullable=False),
        sa.Column("principal_type", sa.Text, nullable=False),
        sa.Column("created_at", sa.Text, nullable=False),
    )
    op.create_table(
        "files",
        sa.Column("namespace_id", sa.Text, nullable=False),
        sa.Column("path", sa.Text, nullable=False),
        sa.Column("current_version_id", sa.Text, nullable=False),
        sa.Column("current_version_number", sa.Integer, nullable=False),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.Column("updated_at", sa.Text, nullable=False),
        sa.Column("is_deleted", _bool(), nullable=False),
        sa.PrimaryKeyConstraint("namespace_id", "path"),
    )
    op.create_table(
        "versions",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("file_path", sa.Text, nullable=False),
        sa.Column("namespace_id", sa.Text, nullable=False),
        sa.Column("version_number", sa.Integer, nullable=False),
        sa.Column("content_hash", sa.Text, nullable=False),
        sa.Column("size", sa.Integer, nullable=False),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.Column("created_by", sa.Text, nullable=False),
        sa.Column("is_tombstone", _bool(), nullable=False),
        sa.Column("search_meta", _json(), nullable=False),
        sa.Column("parent_version_id", sa.Text, nullable=True),
        sa.UniqueConstraint("namespace_id", "file_path", "version_number"),
    )
    op.create_table(
        "permissions",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("principal_id", sa.Text, nullable=False),
        sa.Column("namespace_id", sa.Text, nullable=False),
        sa.Column("path_prefix", sa.Text, nullable=False),
        sa.Column("operations", _json(), nullable=False),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.UniqueConstraint("principal_id", "namespace_id", "path_prefix"),
    )
    op.create_table(
        "audit_events",
        sa.Column("event_id", sa.Text, primary_key=True),
        sa.Column("timestamp", sa.Text, nullable=False),
        sa.Column("namespace_id", sa.Text, nullable=False),
        sa.Column("principal_id", sa.Text, nullable=False),
        sa.Column("operation", sa.Text, nullable=False),
        sa.Column("path", sa.Text, nullable=True),
        sa.Column("version_id", sa.Text, nullable=True),
        sa.Column("detail", _json(), nullable=False),
        sa.Column("trace_id", sa.Text, nullable=True),
    )
    op.create_table(
        "names",
        sa.Column("entity_type", sa.Text, nullable=False),
        sa.Column("entity_id", sa.Text, nullable=False),
        sa.Column("display_name", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("entity_type", "entity_id"),
        sa.UniqueConstraint("entity_type", "display_name"),
    )

    op.create_index(
        "idx_versions_ns_path",
        "versions",
        ["namespace_id", "file_path", sa.text("version_number DESC")],
    )
    op.create_index("idx_permissions_principal", "permissions", ["principal_id", "namespace_id"])
    op.create_index("idx_audit_ns_time", "audit_events", ["namespace_id", sa.text("timestamp DESC")])
    op.create_index("idx_versions_hash", "versions", ["content_hash"])


def downgrade() -> None:
    op.drop_index("idx_versions_hash", table_name="versions")
    op.drop_index("idx_audit_ns_time", table_name="audit_events")
    op.drop_index("idx_permissions_principal", table_name="permissions")
    op.drop_index("idx_versions_ns_path", table_name="versions")
    op.drop_table("names")
    op.drop_table("audit_events")
    op.drop_table("permissions")
    op.drop_table("versions")
    op.drop_table("files")
    op.drop_table("principals")
    op.drop_table("namespaces")
