"""SQLite-backed metadata store, expressed on the shared SQLAlchemy Core schema."""

from __future__ import annotations

from typing import Any, Callable

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

from vfs.stores.sql_metadata import BaseSqlMetadataStore


class SQLiteMetadataStore(BaseSqlMetadataStore):
    """MetadataStore implementation backed by SQLite via SQLAlchemy Core + aiosqlite.

    A thin dialect adapter over :class:`BaseSqlMetadataStore`: it supplies the aiosqlite
    engine (including the ``:memory:`` ``StaticPool`` handling), the SQLite ``insert``
    construct for ``ON CONFLICT`` upserts, and a post-connect step enabling WAL mode.
    All file/version/permission/audit CRUD and CAS live in the shared base.
    """

    def __init__(self, db_path: str) -> None:
        super().__init__()
        self._db_path = db_path

    def _create_engine(self) -> AsyncEngine:
        if self._db_path == ":memory:":
            # A single shared connection keeps the in-memory database alive for the store's lifetime.
            return create_async_engine(
                "sqlite+aiosqlite:///:memory:",
                poolclass=StaticPool,
                connect_args={"check_same_thread": False},
            )
        return create_async_engine(f"sqlite+aiosqlite:///{self._db_path}")

    @property
    def _dialect_insert(self) -> Callable[..., Any]:
        return sqlite_insert

    async def _post_connect(self, conn: AsyncConnection) -> None:
        """Enable write-ahead logging for better concurrency on file-backed databases."""
        await conn.exec_driver_sql("PRAGMA journal_mode=WAL")
