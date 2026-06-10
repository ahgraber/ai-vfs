"""Shared SQLAlchemy Core schema for the SQL metadata adapters.

A single :class:`~sqlalchemy.MetaData` and table set is shared by the SQLite
(``aiosqlite``) and PostgreSQL (``asyncpg``) adapters so both backends use one
schema definition and one CAS implementation.  This module declares tables only
(SQLAlchemy Core) — no ORM session, declarative base, or relationships.

JSON-bearing columns use a dialect variant: a portable JSON type that renders as
``TEXT`` on SQLite and ``JSONB`` on PostgreSQL.  Timestamps are stored as ISO-8601
``TEXT`` and booleans as integer flags so the at-rest representation is identical
across dialects.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


def json_type() -> sa.types.TypeEngine:
    """Portable JSON column type: ``TEXT``-backed JSON on SQLite, ``JSONB`` on PostgreSQL."""
    return sa.JSON().with_variant(JSONB(), "postgresql")


def _bool() -> sa.types.TypeEngine:
    """Boolean stored as a 0/1 integer flag without a CHECK constraint (matches the Phase 1 schema)."""
    return sa.Boolean(create_constraint=False)


metadata = sa.MetaData()

namespaces = sa.Table(
    "namespaces",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("display_name", sa.Text, nullable=False),
    sa.Column("created_at", sa.Text, nullable=False),
    sa.Column("created_by", sa.Text, nullable=False),
    sa.Column("retention_policy", sa.Text, nullable=True),
)

principals = sa.Table(
    "principals",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("display_name", sa.Text, nullable=False),
    sa.Column("principal_type", sa.Text, nullable=False),
    sa.Column("created_at", sa.Text, nullable=False),
)

files = sa.Table(
    "files",
    metadata,
    sa.Column("namespace_id", sa.Text, nullable=False),
    sa.Column("path", sa.Text, nullable=False),
    sa.Column("current_version_id", sa.Text, nullable=False),
    sa.Column("current_version_number", sa.Integer, nullable=False),
    sa.Column("created_at", sa.Text, nullable=False),
    sa.Column("updated_at", sa.Text, nullable=False),
    sa.Column("is_deleted", _bool(), nullable=False, default=False),
    sa.PrimaryKeyConstraint("namespace_id", "path"),
)

versions = sa.Table(
    "versions",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("file_path", sa.Text, nullable=False),
    sa.Column("namespace_id", sa.Text, nullable=False),
    sa.Column("version_number", sa.Integer, nullable=False),
    sa.Column("content_hash", sa.Text, nullable=False),
    sa.Column("size", sa.Integer, nullable=False),
    sa.Column("created_at", sa.Text, nullable=False),
    sa.Column("created_by", sa.Text, nullable=False),
    sa.Column("is_tombstone", _bool(), nullable=False, default=False),
    sa.Column("search_meta", json_type(), nullable=False, default=dict),
    sa.Column("parent_version_id", sa.Text, nullable=True),
    sa.UniqueConstraint("namespace_id", "file_path", "version_number"),
)

permissions = sa.Table(
    "permissions",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("principal_id", sa.Text, nullable=False),
    sa.Column("namespace_id", sa.Text, nullable=False),
    sa.Column("path_prefix", sa.Text, nullable=False),
    sa.Column("operations", json_type(), nullable=False),
    sa.Column("created_at", sa.Text, nullable=False),
    sa.UniqueConstraint("principal_id", "namespace_id", "path_prefix"),
)

audit_events = sa.Table(
    "audit_events",
    metadata,
    sa.Column("event_id", sa.Text, primary_key=True),
    sa.Column("timestamp", sa.Text, nullable=False),
    sa.Column("namespace_id", sa.Text, nullable=False),
    sa.Column("principal_id", sa.Text, nullable=False),
    sa.Column("operation", sa.Text, nullable=False),
    sa.Column("path", sa.Text, nullable=True),
    sa.Column("version_id", sa.Text, nullable=True),
    sa.Column("detail", json_type(), nullable=False, default=dict),
    sa.Column("trace_id", sa.Text, nullable=True),
)

names = sa.Table(
    "names",
    metadata,
    sa.Column("entity_type", sa.Text, nullable=False),
    sa.Column("entity_id", sa.Text, nullable=False),
    sa.Column("display_name", sa.Text, nullable=False),
    sa.PrimaryKeyConstraint("entity_type", "entity_id"),
    sa.UniqueConstraint("entity_type", "display_name"),
)

sa.Index("idx_versions_ns_path", versions.c.namespace_id, versions.c.file_path, versions.c.version_number.desc())
sa.Index("idx_permissions_principal", permissions.c.principal_id, permissions.c.namespace_id)
sa.Index("idx_audit_ns_time", audit_events.c.namespace_id, audit_events.c.timestamp.desc())
sa.Index("idx_versions_hash", versions.c.content_hash)

# --- Native full-text search index ---
#
# CONFIDENTIALITY (classification change): this table stores decoded file content (raw
# text), making the metadata database content-bearing at the same sensitivity tier as the
# blob store.  Protect accordingly: encryption at rest, least-privilege DB roles, restricted
# backups/replicas/analytics access.  Text artifacts are content-addressed and therefore
# shared across namespaces at rest (like blobs); namespace isolation is enforced at the
# query boundary only.  GC MUST delete text artifacts when their content is reclaimed
# (retention/erasure compliance).

search_text_artifacts = sa.Table(
    "search_text_artifacts",
    metadata,
    sa.Column("provider_key", sa.Text, nullable=False),
    sa.Column("params_hash", sa.Text, nullable=False),
    sa.Column("content_hash", sa.Text, nullable=False),
    sa.Column("raw_text", sa.Text, nullable=False),  # decoded file content; content-bearing
    sa.Column("status", sa.Text, nullable=False),  # "ready" | "failed" | "unsupported"
    sa.Column("created_at", sa.Text, nullable=False),
    sa.PrimaryKeyConstraint("provider_key", "params_hash", "content_hash"),
)
# GC orphan check by content_hash (join with blob-GC pass)
sa.Index("idx_sta_content_hash", search_text_artifacts.c.content_hash)
# Retired-params_hash profile sweep
sa.Index("idx_sta_params_hash", search_text_artifacts.c.params_hash)
