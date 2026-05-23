"""Alembic environment for the ai-vfs SQL metadata schema.

Targets both SQLite (aiosqlite at runtime; pysqlite for migrations) and PostgreSQL.
The shared Core schema in :mod:`vfs.stores.schema` is the autogenerate target, so the
JSON columns carry their JSONB-on-PostgreSQL variant into generated migrations.

Migrations run with a synchronous engine; set the database URL via the ``ALEMBIC_DB_URL``
environment variable or ``sqlalchemy.url`` in ``alembic.ini``.
"""

from __future__ import annotations

from logging.config import fileConfig
import os

from alembic import context
from sqlalchemy import create_engine, pool

from vfs.stores.schema import metadata as target_metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _database_url() -> str:
    return os.environ.get("ALEMBIC_DB_URL") or config.get_main_option("sqlalchemy.url")


def run_migrations_offline() -> None:
    """Emit migration SQL without a live connection (``alembic upgrade --sql``)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database connection."""
    engine = create_engine(_database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
