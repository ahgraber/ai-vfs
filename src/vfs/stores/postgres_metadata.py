"""PostgreSQL-backed metadata store, expressed on the shared SQLAlchemy Core schema.

Importable only when the ``postgres`` extra (``asyncpg``) is installed; the URI resolver
guards the import and raises an actionable error otherwise.
"""

from __future__ import annotations

from typing import Any, Callable

from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from vfs.stores.sql_metadata import BaseSqlMetadataStore


class PostgresMetadataStore(BaseSqlMetadataStore):
    """MetadataStore implementation backed by PostgreSQL via SQLAlchemy Core + asyncpg.

    A thin dialect adapter over :class:`BaseSqlMetadataStore`: it supplies the asyncpg
    engine and the PostgreSQL ``insert`` construct for ``ON CONFLICT`` upserts. All
    file/version/permission/audit CRUD, the ``WHERE current_version_number = ?``
    compare-and-swap, and the ``BEGIN``/``COMMIT``/``ROLLBACK`` ``transaction()`` are
    inherited unchanged from the shared base, so SQLite and PostgreSQL share one schema
    and one concurrency-control path. The shared schema renders the ``search_meta`` and
    ``detail`` columns as native ``JSONB`` on PostgreSQL, so those fields round-trip as
    structured JSON automatically.

    Connection model: like the SQLite adapter, a single long-lived
    :class:`~sqlalchemy.ext.asyncio.AsyncConnection` is held for the store's lifetime.
    Every operation runs under the base store's ``asyncio.Lock`` and commits (or rolls
    back) at its own boundary unless wrapped in :meth:`transaction`. This keeps the CAS and
    transaction semantics identical across both SQL backends. The lock serializes
    operations on one store instance; connection pooling for concurrency is a deliberate
    future optimization, not built now.

    Schema creation uses ``metadata.create_all`` (inherited :meth:`initialize`) so the
    store is self-contained for tests; production schema evolution is owned by the Alembic
    migrations.
    """

    def __init__(self, uri: str) -> None:
        """Store the connection URI, translating it to the asyncpg driver for SQLAlchemy.

        The resolver passes the full URI (e.g. ``postgresql://user:pass@host:5432/db``);
        SQLAlchemy needs the ``postgresql+asyncpg`` driver name. No connection is opened
        here — that happens in :meth:`initialize`.

        TLS note: SQLAlchemy's asyncpg dialect expects ``ssl=`` (not libpq's ``sslmode=``)
        in the URL query string, so a ``?sslmode=...`` copied from a psycopg DSN will not
        take effect here. Connection tuning (TLS, pooling) is deferred by design.
        """
        super().__init__()
        self._url = make_url(uri).set(drivername="postgresql+asyncpg")

    def _create_engine(self) -> AsyncEngine:
        return create_async_engine(self._url)

    @property
    def _dialect_insert(self) -> Callable[..., Any]:
        return postgresql_insert
