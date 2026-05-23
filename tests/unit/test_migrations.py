"""Tests that the Alembic initial migration matches the shared Core schema."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
import pytest
import sqlalchemy as sa

from vfs.stores.schema import metadata

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _alembic_config(db_url: str) -> Config:
    cfg = Config(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_REPO_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


@pytest.fixture
def db_url(tmp_path):
    return f"sqlite:///{tmp_path}/migrated.db"


def test_initial_migration_matches_core_schema(db_url):
    """After upgrading to head, the live database has no differences from the Core schema.

    ``compare_metadata`` inspects tables, columns, types, nullability, constraints, and
    indexes, so a non-empty diff means the migration has drifted from ``schema.py``.
    """
    command.upgrade(_alembic_config(db_url), "head")

    engine = sa.create_engine(db_url)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection)
            diff = compare_metadata(context, metadata)
    finally:
        engine.dispose()

    assert diff == [], f"migration drifted from schema.py: {diff}"


def test_migration_downgrade_drops_all_tables(db_url):
    """Downgrading to base removes every schema table (only alembic bookkeeping remains)."""
    cfg = _alembic_config(db_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")

    engine = sa.create_engine(db_url)
    try:
        remaining = [t for t in sa.inspect(engine).get_table_names() if t != "alembic_version"]
    finally:
        engine.dispose()

    assert remaining == []
