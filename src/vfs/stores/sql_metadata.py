"""Shared SQL metadata store on the SQLAlchemy Core schema.

:class:`BaseSqlMetadataStore` holds every dialect-agnostic part of the SQL adapters:
connection lifecycle, all file/version/permission/audit/name CRUD, compare-and-swap,
GC queries, entity persistence, and transaction handling.  Concrete adapters
(:class:`~vfs.stores.sqlite_metadata.SQLiteMetadataStore`,
:class:`~vfs.stores.postgres_metadata.PostgresMetadataStore`) supply only the dialect
hooks: how to build the engine, which ``insert`` construct implements ``ON CONFLICT``,
and an optional post-connect step.

The single shared schema (``vfs.stores.schema``) and the single CAS implementation here
are the point of the SQLAlchemy-Core decision in the Phase 2 design: SQLite and PostgreSQL
share one schema and one concurrency-control path rather than two divergent ones.
"""

from __future__ import annotations

import abc
import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime
import json
from typing import Any, AsyncIterator, Callable, Mapping, Sequence

import sqlalchemy as sa
from sqlalchemy import delete, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from vfs.errors import ConflictError
from vfs.models import (
    AuditEvent,
    FileMeta,
    Namespace,
    Permission,
    Principal,
    RetentionPolicy,
    VersionMeta,
)
from vfs.stores.schema import (
    audit_events,
    files,
    metadata,
    names,
    namespaces,
    permissions,
    principals,
    versions,
)

# Task-local marker for "this coroutine is currently inside transaction()". It MUST be a
# ContextVar, not an instance bool: a plain bool is shared across concurrent tasks, so a
# task that has *not* entered transaction() could observe another task's True and wrongly
# skip the lock/commit. ContextVar scopes the flag to the task that owns the transaction.
_in_txn: ContextVar[bool] = ContextVar("aivfs_sql_in_txn", default=False)


class BaseSqlMetadataStore(abc.ABC):
    """Dialect-agnostic MetadataStore backed by SQLAlchemy Core.

    Holds a single long-lived :class:`AsyncConnection` for the store's lifetime so an
    in-memory database and explicit transactions both work against one connection.

    Concurrency model: the single connection cannot be shared safely by concurrent
    coroutines, so every operation runs under ``self._lock`` and is given a
    commit/rollback boundary by :meth:`_operation`. This serializes all operations on one
    store instance. A per-instance connection pool that would allow true concurrency is a
    deliberate future optimization, not built now.
    """

    def __init__(self) -> None:
        self._engine: AsyncEngine | None = None
        self._conn: AsyncConnection | None = None
        # Serializes access to the single shared connection across concurrent coroutines.
        self._lock = asyncio.Lock()

    # --- Dialect hooks (subclass responsibilities) ---

    @abc.abstractmethod
    def _create_engine(self) -> AsyncEngine:
        """Return the dialect-specific async engine."""

    @property
    @abc.abstractmethod
    def _dialect_insert(self) -> Callable[..., Any]:
        """Return the dialect ``insert`` construct exposing ``on_conflict_do_update``.

        For SQLite this is :func:`sqlalchemy.dialects.sqlite.insert`; for PostgreSQL it is
        :func:`sqlalchemy.dialects.postgresql.insert`.
        """

    async def _post_connect(self, conn: AsyncConnection) -> None:  # noqa: B027 — intentional no-op default
        """Run dialect-specific setup on a freshly opened connection (default: no-op)."""

    # --- Lifecycle ---

    async def initialize(self) -> None:
        """Open the connection, run the post-connect hook, and create tables and indexes.

        ``metadata.create_all`` keeps the store self-contained for tests and the
        default profile; production schema evolution is owned by the Alembic migrations.
        """
        async with self._lock:
            self._engine = self._create_engine()
            self._conn = await self._engine.connect()
            await self._post_connect(self._conn)
            await self._conn.run_sync(metadata.create_all)
            await self._conn.commit()

    async def close(self) -> None:
        """Close the held connection and dispose the engine."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None

    @property
    def _db(self) -> AsyncConnection:
        if self._conn is None:
            raise RuntimeError(f"{type(self).__name__} is not initialized; call initialize() first")
        return self._conn

    # --- Internal helpers ---

    @asynccontextmanager
    async def _operation(self) -> AsyncIterator[None]:
        """Give a single store operation a commit/rollback boundary on the shared connection.

        Outside :meth:`transaction`, acquires ``self._lock`` (serializing concurrent
        coroutines on the one connection), runs the operation, then commits on success or
        rolls back on any exception — ending the connection's transaction so reads do not
        leave it idle-in-transaction. Inside :meth:`transaction` (detected via the
        task-local ``_in_txn`` ContextVar) the held connection is reused without an extra
        lock or commit: the surrounding ``transaction()`` owns commit/rollback.
        """
        if _in_txn.get():
            yield
        else:
            async with self._lock:
                try:
                    yield
                    await self._db.commit()
                except Exception:
                    await self._db.rollback()
                    raise

    def _upsert(
        self,
        table: sa.Table,
        values: Mapping[str, Any],
        *,
        index_elements: Sequence[str],
        set_: Sequence[str],
    ) -> sa.sql.dml.Insert:
        """Build a dialect-agnostic ``INSERT ... ON CONFLICT DO UPDATE`` statement.

        ``index_elements`` names the conflict-target columns; ``set_`` names the columns
        to overwrite with the proposed (``excluded``) values on conflict.
        """
        stmt = self._dialect_insert(table).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=list(index_elements),
            set_={col: getattr(stmt.excluded, col) for col in set_},
        )
        return stmt

    async def _execute_fetchall(self, sql: str, params: tuple = ()) -> Sequence[sa.Row]:
        result = await self._db.exec_driver_sql(sql, params)
        return result.fetchall()

    async def _execute_fetchone(self, sql: str, params: tuple = ()) -> sa.Row | None:
        result = await self._db.exec_driver_sql(sql, params)
        return result.fetchone()

    # --- File operations ---

    async def put_file(self, file_meta: FileMeta) -> None:
        """Insert or replace a file record."""
        async with self._operation():
            await self._db.execute(
                self._upsert(
                    files,
                    {
                        "namespace_id": file_meta.namespace_id,
                        "path": file_meta.path,
                        "current_version_id": file_meta.current_version_id,
                        "current_version_number": file_meta.current_version_number,
                        "created_at": file_meta.created_at.isoformat(),
                        "updated_at": file_meta.updated_at.isoformat(),
                        "is_deleted": file_meta.is_deleted,
                    },
                    index_elements=["namespace_id", "path"],
                    set_=["current_version_id", "current_version_number", "updated_at", "is_deleted"],
                )
            )

    async def get_file(self, namespace_id: str, path: str) -> FileMeta | None:
        """Return the file record for namespace_id/path, or None if absent."""
        async with self._operation():
            row = (
                await self._db.execute(select(files).where(files.c.namespace_id == namespace_id, files.c.path == path))
            ).first()
        if row is None:
            return None
        return self._row_to_file(row)

    async def delete_file(self, namespace_id: str, path: str) -> None:
        """Hard-delete the file record (use put_version with is_tombstone for soft delete)."""
        async with self._operation():
            await self._db.execute(delete(files).where(files.c.namespace_id == namespace_id, files.c.path == path))

    async def list_dir(self, namespace_id: str, path_prefix: str, *, recursive: bool = False) -> list[FileMeta]:
        """List live (non-deleted) files under path_prefix; recurse into subdirectories when recursive=True."""
        async with self._operation():
            rows = (
                await self._db.execute(
                    select(files).where(
                        files.c.namespace_id == namespace_id,
                        files.c.path.like(path_prefix + "%"),
                        files.c.is_deleted == False,  # noqa: E712 — SQL boolean comparison, not a Python identity check
                    )
                )
            ).fetchall()
        results = []
        for row in rows:
            path = row.path
            if not recursive:
                # Non-recursive: exclude paths with additional '/' after prefix
                remainder = path[len(path_prefix) :]
                if "/" in remainder:
                    continue
            results.append(self._row_to_file(row))
        return results

    # --- Version operations ---

    async def put_version(self, version: VersionMeta, *, expected_version: int | None = None) -> None:
        """Persist a new version and advance the file's current-version pointer.

        When ``expected_version`` is set, the pointer is advanced with a
        ``WHERE current_version_number = expected_version`` compare-and-swap. If that matches
        no row — the file was concurrently advanced, or does not exist — ``ConflictError`` is
        raised at the write site; the enclosing :meth:`_operation` rolls the pending write
        back, leaving no orphan version row. The CAS update precedes the version insert so a
        mismatch inserts nothing.
        """
        now = version.created_at.isoformat()
        version_values = {
            "id": version.id,
            "file_path": version.file_path,
            "namespace_id": version.namespace_id,
            "version_number": version.version_number,
            "content_hash": version.content_hash,
            "size": version.size,
            "created_at": now,
            "created_by": version.created_by,
            "is_tombstone": version.is_tombstone,
            "search_meta": version.search_meta,
            "parent_version_id": version.parent_version_id,
        }

        async with self._operation():
            if expected_version is None:
                # New file or unconditional upsert: insert the version, then upsert the pointer.
                await self._db.execute(insert(versions).values(**version_values))
                await self._db.execute(
                    self._upsert(
                        files,
                        {
                            "namespace_id": version.namespace_id,
                            "path": version.file_path,
                            "current_version_id": version.id,
                            "current_version_number": version.version_number,
                            "created_at": now,
                            "updated_at": now,
                            "is_deleted": version.is_tombstone,
                        },
                        index_elements=["namespace_id", "path"],
                        set_=["current_version_id", "current_version_number", "updated_at", "is_deleted"],
                    )
                )
            else:
                # Compare-and-swap at the write site: advance the pointer only while the file is
                # still at expected_version. Zero matched rows => conflict.
                result = await self._db.execute(
                    update(files)
                    .where(
                        files.c.namespace_id == version.namespace_id,
                        files.c.path == version.file_path,
                        files.c.current_version_number == expected_version,
                    )
                    .values(
                        current_version_id=version.id,
                        current_version_number=version.version_number,
                        updated_at=now,
                        is_deleted=version.is_tombstone,
                    )
                )
                if result.rowcount == 0:
                    # Raise inside _operation(): the wrapper rolls back the (empty) CAS write,
                    # so no orphan version row is left behind.
                    raise ConflictError(
                        f"CAS conflict: expected version {expected_version} "
                        f"for {version.namespace_id}:{version.file_path}"
                    )
                await self._db.execute(insert(versions).values(**version_values))

    async def get_version(self, namespace_id: str, path: str, version_number: int | None = None) -> VersionMeta | None:
        """Return the specified version, or the latest when version_number is None."""
        stmt = select(versions).where(versions.c.namespace_id == namespace_id, versions.c.file_path == path)
        if version_number is None:
            stmt = stmt.order_by(versions.c.version_number.desc()).limit(1)
        else:
            stmt = stmt.where(versions.c.version_number == version_number)
        async with self._operation():
            row = (await self._db.execute(stmt)).first()
        if row is None:
            return None
        return self._row_to_version(row)

    async def list_versions(
        self,
        namespace_id: str,
        path: str,
        *,
        limit: int = 50,
        before: int | None = None,
    ) -> list[VersionMeta]:
        """Return up to limit versions, newest-first; cursor-paginate with before."""
        stmt = select(versions).where(versions.c.namespace_id == namespace_id, versions.c.file_path == path)
        if before is not None:
            stmt = stmt.where(versions.c.version_number < before)
        stmt = stmt.order_by(versions.c.version_number.desc()).limit(limit)
        async with self._operation():
            rows = (await self._db.execute(stmt)).fetchall()
        return [self._row_to_version(row) for row in rows]

    # --- Permissions ---

    async def check_permission(self, principal_id: str, namespace_id: str, path: str, operation: str) -> bool:
        """Return True if the principal's most-specific matching rule allows operation."""
        async with self._operation():
            rows = (
                await self._db.execute(
                    select(permissions.c.path_prefix, permissions.c.operations).where(
                        permissions.c.principal_id == principal_id,
                        permissions.c.namespace_id == namespace_id,
                    )
                )
            ).fetchall()
        if not rows:
            return False
        # Sort by path_prefix length descending (most-specific first)
        rows = sorted(rows, key=lambda r: len(r.path_prefix), reverse=True)
        for row in rows:
            if path.startswith(row.path_prefix):
                return operation in set(row.operations)
        return False

    async def set_permission(self, permission: Permission) -> None:
        """Insert or replace the permission entry for the given (principal, namespace, path_prefix) scope."""
        async with self._operation():
            await self._db.execute(
                self._upsert(
                    permissions,
                    {
                        "id": permission.id,
                        "principal_id": permission.principal_id,
                        "namespace_id": permission.namespace_id,
                        "path_prefix": permission.path_prefix,
                        "operations": sorted(permission.operations),
                        "created_at": permission.created_at.isoformat(),
                    },
                    index_elements=["principal_id", "namespace_id", "path_prefix"],
                    set_=["id", "operations", "created_at"],
                )
            )

    async def has_any_admin(self, namespace_id: str) -> bool:
        """Return True if any permission row in the namespace lists `admin` among its operations."""
        async with self._operation():
            rows = (
                await self._db.execute(
                    select(permissions.c.operations).where(permissions.c.namespace_id == namespace_id)
                )
            ).fetchall()
        return any("admin" in set(row.operations) for row in rows)

    # --- Audit ---

    async def append_audit_event(self, event: AuditEvent) -> None:
        """Append an immutable audit record."""
        async with self._operation():
            await self._db.execute(
                insert(audit_events).values(
                    event_id=event.event_id,
                    timestamp=event.timestamp.isoformat(),
                    namespace_id=event.namespace_id,
                    principal_id=event.principal_id,
                    operation=event.operation,
                    path=event.path,
                    version_id=event.version_id,
                    detail=event.detail,
                    trace_id=event.trace_id,
                )
            )

    # --- Search metadata ---

    async def update_search_meta(self, version_id: str, search_meta: dict) -> None:
        """Update the search_meta field on a version record."""
        async with self._operation():
            await self._db.execute(update(versions).where(versions.c.id == version_id).values(search_meta=search_meta))

    # --- Name resolution ---

    async def set_name(self, entity_type: str, entity_id: str, display_name: str) -> None:
        """Register or replace the display name for an entity.

        Registering or renaming the *same* entity updates its display name (the upsert's
        ``ON CONFLICT (entity_type, entity_id) DO UPDATE`` handles the PK conflict).
        Claiming a ``display_name`` already held by a *different* entity of the same
        ``entity_type`` violates the ``UNIQUE(entity_type, display_name)`` constraint and
        raises :class:`~vfs.errors.ConflictError` rather than leaking a raw DB error.

        On the unique-constraint violation the :class:`~sqlalchemy.exc.IntegrityError` is
        translated to ``ConflictError`` and re-raised inside :meth:`_operation`, which rolls
        the failed statement back — keeping the connection usable (notably on Postgres,
        where the aborted statement otherwise poisons the transaction).
        """
        async with self._operation():
            try:
                await self._db.execute(
                    self._upsert(
                        names,
                        {"entity_type": entity_type, "entity_id": entity_id, "display_name": display_name},
                        index_elements=["entity_type", "entity_id"],
                        set_=["display_name"],
                    )
                )
            except sa.exc.IntegrityError as exc:
                # The display-name unique constraint is the only IntegrityError reachable here;
                # the PK conflict is absorbed by the upsert's DO UPDATE above. Re-raise as
                # ConflictError inside _operation() so the wrapper rolls back.
                raise ConflictError(
                    f"display name {display_name!r} is already in use for entity_type {entity_type!r}"
                ) from exc

    async def resolve_name(self, entity_type: str, display_name: str) -> str | None:
        """Return the entity ID for a display name, or None if not found."""
        async with self._operation():
            row = (
                await self._db.execute(
                    select(names.c.entity_id).where(
                        names.c.entity_type == entity_type,
                        names.c.display_name == display_name,
                    )
                )
            ).first()
        return row[0] if row else None

    # --- GC ---

    async def list_reclaimable_versions(
        self, policy: RetentionPolicy, namespace_id: str | None = None
    ) -> list[VersionMeta]:
        """Return non-tombstone versions exceeding the retention policy, excluding version 1 and the current version."""
        file_select = select(files.c.namespace_id, files.c.path)
        if namespace_id:
            file_select = file_select.where(files.c.namespace_id == namespace_id)

        reclaimable: list[VersionMeta] = []
        async with self._operation():
            file_rows = (await self._db.execute(file_select)).fetchall()
            for file_row in file_rows:
                version_rows = (
                    await self._db.execute(
                        select(versions)
                        .where(
                            versions.c.namespace_id == file_row.namespace_id,
                            versions.c.file_path == file_row.path,
                            versions.c.is_tombstone == False,  # noqa: E712 — SQL boolean comparison
                        )
                        .order_by(versions.c.version_number.desc())
                    )
                ).fetchall()
                if len(version_rows) <= policy.max_recent_versions:
                    continue
                # Keep the N most recent
                excess = version_rows[policy.max_recent_versions :]
                for row in excess:
                    ver = self._row_to_version(row)
                    # Always keep first version if configured
                    if policy.keep_first_version and ver.version_number == 1:
                        continue
                    reclaimable.append(ver)
        return reclaimable

    async def delete_versions(self, version_ids: list[str]) -> None:
        """Hard-delete version records by ID."""
        if not version_ids:
            return
        async with self._operation():
            await self._db.execute(delete(versions).where(versions.c.id.in_(version_ids)))

    async def has_version_references(self, content_hash: str) -> bool:
        """Return True if any version record references the given content hash."""
        async with self._operation():
            row = (
                await self._db.execute(select(versions.c.id).where(versions.c.content_hash == content_hash).limit(1))
            ).first()
        return row is not None

    # --- Entity persistence ---

    async def put_namespace(self, namespace: Namespace) -> None:
        """Persist a namespace record."""
        retention = (
            json.dumps(namespace.retention_policy.model_dump(), default=str) if namespace.retention_policy else None
        )
        async with self._operation():
            await self._db.execute(
                self._upsert(
                    namespaces,
                    {
                        "id": namespace.id,
                        "display_name": namespace.display_name,
                        "created_at": namespace.created_at.isoformat(),
                        "created_by": namespace.created_by,
                        "retention_policy": retention,
                    },
                    index_elements=["id"],
                    set_=["display_name", "created_at", "created_by", "retention_policy"],
                )
            )

    async def put_principal(self, principal: Principal) -> None:
        """Persist a principal record."""
        async with self._operation():
            await self._db.execute(
                self._upsert(
                    principals,
                    {
                        "id": principal.id,
                        "display_name": principal.display_name,
                        "principal_type": principal.principal_type,
                        "created_at": principal.created_at.isoformat(),
                    },
                    index_elements=["id"],
                    set_=["display_name", "principal_type", "created_at"],
                )
            )

    # --- Transactions ---

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        """Async context manager for atomic multi-step operations; rolls back on exception.

        Holds ``self._lock`` for the whole block, so all writes share one transaction on the
        held connection and the transaction is exclusive: operations issued from *other*
        tasks block on the lock until this transaction commits or rolls back, so no write
        leaks across tasks. The task-local ``_in_txn`` ContextVar tells :meth:`_operation`
        to reuse the held connection without re-locking or committing.

        Because the single connection + lock serialize operations per store instance, the
        operations *within* a transaction must run sequentially in the same task; spawning
        concurrent store operations inside a ``transaction()`` block is unsupported (they
        would deadlock on the held lock).
        """
        async with self._lock:
            token = _in_txn.set(True)
            try:
                yield
                await self._db.commit()
            except Exception:
                await self._db.rollback()
                raise
            finally:
                _in_txn.reset(token)

    # --- Row mapping helpers ---

    @staticmethod
    def _row_to_file(row: sa.Row) -> FileMeta:
        return FileMeta(
            namespace_id=row.namespace_id,
            path=row.path,
            current_version_id=row.current_version_id,
            current_version_number=row.current_version_number,
            created_at=datetime.fromisoformat(row.created_at),
            updated_at=datetime.fromisoformat(row.updated_at),
            is_deleted=bool(row.is_deleted),
        )

    @staticmethod
    def _row_to_version(row: sa.Row) -> VersionMeta:
        return VersionMeta(
            id=row.id,
            file_path=row.file_path,
            namespace_id=row.namespace_id,
            version_number=row.version_number,
            content_hash=row.content_hash,
            size=row.size,
            created_at=datetime.fromisoformat(row.created_at),
            created_by=row.created_by,
            is_tombstone=bool(row.is_tombstone),
            search_meta=row.search_meta if row.search_meta else {},
            parent_version_id=row.parent_version_id,
        )
