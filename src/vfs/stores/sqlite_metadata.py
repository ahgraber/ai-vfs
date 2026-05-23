"""SQLite-backed metadata store, expressed on the shared SQLAlchemy Core schema."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
import json
from typing import AsyncIterator, Sequence

import sqlalchemy as sa
from sqlalchemy import delete, insert, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

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


class SQLiteMetadataStore:
    """MetadataStore implementation backed by SQLite via SQLAlchemy Core + aiosqlite.

    Holds a single long-lived :class:`AsyncConnection` for the store's lifetime so an
    in-memory database (``:memory:``) and explicit transactions both work against one
    connection.  Operations auto-commit unless wrapped in :meth:`transaction`.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._engine: AsyncEngine | None = None
        self._conn: AsyncConnection | None = None
        self._in_transaction: bool = False

    def _create_engine(self) -> AsyncEngine:
        if self._db_path == ":memory:":
            # A single shared connection keeps the in-memory database alive for the store's lifetime.
            return create_async_engine(
                "sqlite+aiosqlite:///:memory:",
                poolclass=StaticPool,
                connect_args={"check_same_thread": False},
            )
        return create_async_engine(f"sqlite+aiosqlite:///{self._db_path}")

    async def initialize(self) -> None:
        """Open the connection, enable WAL mode, and create tables and indexes."""
        self._engine = self._create_engine()
        self._conn = await self._engine.connect()
        await self._conn.exec_driver_sql("PRAGMA journal_mode=WAL")
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
            raise RuntimeError("SQLiteMetadataStore is not initialized; call initialize() first")
        return self._conn

    # --- Internal helpers ---

    async def _auto_commit(self) -> None:
        """Commit unless we're inside an explicit transaction."""
        if not self._in_transaction:
            await self._db.commit()

    async def _execute_fetchall(self, sql: str, params: tuple = ()) -> Sequence[sa.Row]:
        result = await self._db.exec_driver_sql(sql, params)
        return result.fetchall()

    async def _execute_fetchone(self, sql: str, params: tuple = ()) -> sa.Row | None:
        result = await self._db.exec_driver_sql(sql, params)
        return result.fetchone()

    # --- File operations ---

    async def put_file(self, file_meta: FileMeta) -> None:
        """Insert or replace a file record."""
        stmt = sqlite_insert(files).values(
            namespace_id=file_meta.namespace_id,
            path=file_meta.path,
            current_version_id=file_meta.current_version_id,
            current_version_number=file_meta.current_version_number,
            created_at=file_meta.created_at.isoformat(),
            updated_at=file_meta.updated_at.isoformat(),
            is_deleted=file_meta.is_deleted,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["namespace_id", "path"],
            set_={
                "current_version_id": stmt.excluded.current_version_id,
                "current_version_number": stmt.excluded.current_version_number,
                "updated_at": stmt.excluded.updated_at,
                "is_deleted": stmt.excluded.is_deleted,
            },
        )
        await self._db.execute(stmt)
        await self._auto_commit()

    async def get_file(self, namespace_id: str, path: str) -> FileMeta | None:
        """Return the file record for namespace_id/path, or None if absent."""
        row = (
            await self._db.execute(select(files).where(files.c.namespace_id == namespace_id, files.c.path == path))
        ).first()
        if row is None:
            return None
        return self._row_to_file(row)

    async def delete_file(self, namespace_id: str, path: str) -> None:
        """Hard-delete the file record (use put_version with is_tombstone for soft delete)."""
        await self._db.execute(delete(files).where(files.c.namespace_id == namespace_id, files.c.path == path))
        await self._auto_commit()

    async def list_dir(self, namespace_id: str, path_prefix: str, *, recursive: bool = False) -> list[FileMeta]:
        """List live (non-deleted) files under path_prefix; recurse into subdirectories when recursive=True."""
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
        no row — the file was concurrently advanced, or does not exist — the pending write is
        rolled back and ``ConflictError`` is raised at the write site, leaving no orphan
        version row. The CAS update precedes the version insert so a mismatch inserts nothing.
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

        if expected_version is None:
            # New file or unconditional upsert: insert the version, then upsert the pointer.
            await self._db.execute(insert(versions).values(**version_values))
            stmt = sqlite_insert(files).values(
                namespace_id=version.namespace_id,
                path=version.file_path,
                current_version_id=version.id,
                current_version_number=version.version_number,
                created_at=now,
                updated_at=now,
                is_deleted=version.is_tombstone,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["namespace_id", "path"],
                set_={
                    "current_version_id": stmt.excluded.current_version_id,
                    "current_version_number": stmt.excluded.current_version_number,
                    "updated_at": stmt.excluded.updated_at,
                    "is_deleted": stmt.excluded.is_deleted,
                },
            )
            await self._db.execute(stmt)
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
                if not self._in_transaction:
                    await self._db.rollback()
                raise ConflictError(
                    f"CAS conflict: expected version {expected_version} for {version.namespace_id}:{version.file_path}"
                )
            await self._db.execute(insert(versions).values(**version_values))

        await self._auto_commit()

    async def get_version(self, namespace_id: str, path: str, version_number: int | None = None) -> VersionMeta | None:
        """Return the specified version, or the latest when version_number is None."""
        stmt = select(versions).where(versions.c.namespace_id == namespace_id, versions.c.file_path == path)
        if version_number is None:
            stmt = stmt.order_by(versions.c.version_number.desc()).limit(1)
        else:
            stmt = stmt.where(versions.c.version_number == version_number)
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
        rows = (await self._db.execute(stmt)).fetchall()
        return [self._row_to_version(row) for row in rows]

    # --- Permissions ---

    async def check_permission(self, principal_id: str, namespace_id: str, path: str, operation: str) -> bool:
        """Return True if the principal's most-specific matching rule allows operation."""
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
        stmt = sqlite_insert(permissions).values(
            id=permission.id,
            principal_id=permission.principal_id,
            namespace_id=permission.namespace_id,
            path_prefix=permission.path_prefix,
            operations=sorted(permission.operations),
            created_at=permission.created_at.isoformat(),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["principal_id", "namespace_id", "path_prefix"],
            set_={
                "id": stmt.excluded.id,
                "operations": stmt.excluded.operations,
                "created_at": stmt.excluded.created_at,
            },
        )
        await self._db.execute(stmt)
        await self._auto_commit()

    async def has_any_admin(self, namespace_id: str) -> bool:
        """Return True if any permission row in the namespace lists `admin` among its operations."""
        rows = (
            await self._db.execute(select(permissions.c.operations).where(permissions.c.namespace_id == namespace_id))
        ).fetchall()
        return any("admin" in set(row.operations) for row in rows)

    # --- Audit ---

    async def append_audit_event(self, event: AuditEvent) -> None:
        """Append an immutable audit record."""
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
        await self._auto_commit()

    # --- Search metadata ---

    async def update_search_meta(self, version_id: str, search_meta: dict) -> None:
        """Update the search_meta field on a version record."""
        await self._db.execute(update(versions).where(versions.c.id == version_id).values(search_meta=search_meta))
        await self._auto_commit()

    # --- Name resolution ---

    async def set_name(self, entity_type: str, entity_id: str, display_name: str) -> None:
        """Register or replace the display name for an entity."""
        await self._db.execute(
            insert(names)
            .prefix_with("OR REPLACE")
            .values(entity_type=entity_type, entity_id=entity_id, display_name=display_name)
        )
        await self._auto_commit()

    async def resolve_name(self, entity_type: str, display_name: str) -> str | None:
        """Return the entity ID for a display name, or None if not found."""
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
        file_rows = (await self._db.execute(file_select)).fetchall()

        reclaimable: list[VersionMeta] = []
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
        await self._db.execute(delete(versions).where(versions.c.id.in_(version_ids)))
        await self._auto_commit()

    async def has_version_references(self, content_hash: str) -> bool:
        """Return True if any version record references the given content hash."""
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
        await self._db.execute(
            insert(namespaces)
            .prefix_with("OR REPLACE")
            .values(
                id=namespace.id,
                display_name=namespace.display_name,
                created_at=namespace.created_at.isoformat(),
                created_by=namespace.created_by,
                retention_policy=retention,
            )
        )
        await self._auto_commit()

    async def put_principal(self, principal: Principal) -> None:
        """Persist a principal record."""
        await self._db.execute(
            insert(principals)
            .prefix_with("OR REPLACE")
            .values(
                id=principal.id,
                display_name=principal.display_name,
                principal_type=principal.principal_type,
                created_at=principal.created_at.isoformat(),
            )
        )
        await self._auto_commit()

    # --- Transactions ---

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        """Async context manager for atomic multi-step operations; rolls back on exception.

        Suppresses per-operation auto-commit so all writes share one transaction on the
        held connection, committing on success and rolling back on any exception.
        """
        self._in_transaction = True
        try:
            yield
            await self._db.commit()
        except Exception:
            await self._db.rollback()
            raise
        finally:
            self._in_transaction = False

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
