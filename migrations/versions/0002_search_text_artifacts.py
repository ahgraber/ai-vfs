"""add search_text_artifacts table

Revision ID: 0002_search_text_artifacts
Revises: 0001_initial_schema
Create Date: 2026-06-09

Adds the content-addressed text-index table used by the NativeTextSearch capability on
SQLite (FTS5) and PostgreSQL (tsvector + pg_trgm).

CONFIDENTIALITY: ``search_text_artifacts`` stores decoded file content, making the
metadata database content-bearing at blob-store sensitivity.  Protect accordingly.

Dialect-specific derived indexes:

* **PostgreSQL**: a GIN index using ``gin_trgm_ops`` on ``raw_text`` (created here via
  ``pg_trgm`` extension) accelerates ``text ~ :pattern`` regex queries.  The ``tsvector``
  used for fulltext ranking is computed inline at query time (``to_tsvector()``) rather
  than stored, to keep the schema cross-dialect.
* **SQLite**: the FTS5 virtual table ``search_fts`` is a *derived* index created by
  ``SQLiteMetadataStore.initialize()`` — not here — because ``CREATE VIRTUAL TABLE`` with
  ``tokenize='trigram'`` requires SQLite ≥ 3.34 and would break older environments that
  only use this migration for schema setup.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0002_search_text_artifacts"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "search_text_artifacts",
        sa.Column("provider_key", sa.Text, nullable=False),
        sa.Column("params_hash", sa.Text, nullable=False),
        sa.Column("content_hash", sa.Text, nullable=False),
        sa.Column("raw_text", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("provider_key", "params_hash", "content_hash"),
    )
    op.create_index("idx_sta_content_hash", "search_text_artifacts", ["content_hash"])
    op.create_index("idx_sta_params_hash", "search_text_artifacts", ["params_hash"])

    # Postgres-only: enable pg_trgm and create a GIN index for accelerated regex queries.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        bind.execute(sa.text("CREATE INDEX idx_sta_trgm ON search_text_artifacts USING gin(raw_text gin_trgm_ops)"))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.execute(sa.text("DROP INDEX IF EXISTS idx_sta_trgm"))
    op.drop_index("idx_sta_params_hash", table_name="search_text_artifacts")
    op.drop_index("idx_sta_content_hash", table_name="search_text_artifacts")
    op.drop_table("search_text_artifacts")
