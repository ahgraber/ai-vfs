"""SQLite-backed metadata store."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
import json
import textwrap
from typing import AsyncIterator

import aiosqlite

from vfs.errors import ConflictError, NotFoundError
from vfs.models import (
    AuditEvent,
    FileMeta,
    Namespace,
    Permission,
    Principal,
    RetentionPolicy,
    VersionMeta,
)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS namespaces (
    id          TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    created_by  TEXT NOT NULL,
    retention_policy TEXT
);

CREATE TABLE IF NOT EXISTS principals (
    id          TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    principal_type TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    namespace_id         TEXT NOT NULL,
    path                 TEXT NOT NULL,
    current_version_id   TEXT NOT NULL,
    current_version_number INTEGER NOT NULL,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    is_deleted           INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (namespace_id, path)
);

CREATE TABLE IF NOT EXISTS versions (
    id               TEXT PRIMARY KEY,
    file_path        TEXT NOT NULL,
    namespace_id     TEXT NOT NULL,
    version_number   INTEGER NOT NULL,
    content_hash     TEXT NOT NULL,
    size             INTEGER NOT NULL,
    created_at       TEXT NOT NULL,
    created_by       TEXT NOT NULL,
    is_tombstone     INTEGER NOT NULL DEFAULT 0,
    search_meta      TEXT NOT NULL DEFAULT '{}',
    parent_version_id TEXT,
    UNIQUE (namespace_id, file_path, version_number)
);

CREATE TABLE IF NOT EXISTS permissions (
    id           TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL,
    namespace_id TEXT NOT NULL,
    path_prefix  TEXT NOT NULL,
    operations   TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    UNIQUE (principal_id, namespace_id, path_prefix)
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id     TEXT PRIMARY KEY,
    timestamp    TEXT NOT NULL,
    namespace_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    operation    TEXT NOT NULL,
    path         TEXT,
    version_id   TEXT,
    detail       TEXT NOT NULL DEFAULT '{}',
    trace_id     TEXT
);

CREATE TABLE IF NOT EXISTS names (
    entity_type  TEXT NOT NULL,
    entity_id    TEXT NOT NULL,
    display_name TEXT NOT NULL,
    PRIMARY KEY (entity_type, entity_id),
    UNIQUE (entity_type, display_name)
);

CREATE INDEX IF NOT EXISTS idx_versions_ns_path
    ON versions (namespace_id, file_path, version_number DESC);
CREATE INDEX IF NOT EXISTS idx_permissions_principal
    ON permissions (principal_id, namespace_id);
CREATE INDEX IF NOT EXISTS idx_audit_ns_time
    ON audit_events (namespace_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_versions_hash
    ON versions (content_hash);
"""


class SQLiteMetadataStore:
    """MetadataStore implementation backed by SQLite via aiosqlite."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None
        self._in_transaction: bool = False

    async def initialize(self) -> None:
        """Create tables and indexes; enable WAL mode."""
        self._conn = await aiosqlite.connect(self._db_path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.executescript(_SCHEMA_SQL)
        await self._conn.commit()

    async def close(self) -> None:
        """Close the underlying aiosqlite connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def _db(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("SQLiteMetadataStore is not initialized; call initialize() first")
        return self._conn

    # --- Internal helpers ---

    async def _auto_commit(self) -> None:
        """Commit unless we're inside an explicit transaction."""
        if not self._in_transaction:
            await self._conn.commit()

    async def _execute_fetchall(self, sql: str, params: tuple = ()) -> list[tuple]:
        cursor = await self._db.execute(sql, params)
        return await cursor.fetchall()

    async def _execute_fetchone(self, sql: str, params: tuple = ()) -> tuple | None:
        cursor = await self._db.execute(sql, params)
        return await cursor.fetchone()

    # --- File operations ---

    async def put_file(self, file_meta: FileMeta) -> None:
        """Insert or replace a file record."""
        await self._db.execute(
            """
INSERT INTO files (namespace_id, path, current_version_id,
    current_version_number, created_at, updated_at, is_deleted)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(namespace_id, path) DO UPDATE SET
    current_version_id=excluded.current_version_id,
    current_version_number=excluded.current_version_number,
    updated_at=excluded.updated_at,
    is_deleted=excluded.is_deleted
""".strip(),
            (
                file_meta.namespace_id,
                file_meta.path,
                file_meta.current_version_id,
                file_meta.current_version_number,
                file_meta.created_at.isoformat(),
                file_meta.updated_at.isoformat(),
                int(file_meta.is_deleted),
            ),
        )
        await self._auto_commit()

    async def get_file(self, namespace_id: str, path: str) -> FileMeta | None:
        """Return the file record for namespace_id/path, or None if absent."""
        row = await self._execute_fetchone(
            "SELECT * FROM files WHERE namespace_id=? AND path=?",
            (namespace_id, path),
        )
        if row is None:
            return None
        return FileMeta(
            namespace_id=row[0],
            path=row[1],
            current_version_id=row[2],
            current_version_number=row[3],
            created_at=datetime.fromisoformat(row[4]),
            updated_at=datetime.fromisoformat(row[5]),
            is_deleted=bool(row[6]),
        )

    async def delete_file(self, namespace_id: str, path: str) -> None:
        """Hard-delete the file record (use put_version with is_tombstone for soft delete)."""
        await self._db.execute(
            "DELETE FROM files WHERE namespace_id=? AND path=?",
            (namespace_id, path),
        )
        await self._auto_commit()

    async def list_dir(self, namespace_id: str, path_prefix: str, *, recursive: bool = False) -> list[FileMeta]:
        """List live (non-deleted) files under path_prefix; recurse into subdirectories when recursive=True."""
        rows = await self._execute_fetchall(
            "SELECT * FROM files WHERE namespace_id=? AND path LIKE ? AND is_deleted=0",
            (namespace_id, path_prefix + "%"),
        )
        results = []
        for row in rows:
            path = row[1]
            if not recursive:
                # Non-recursive: exclude paths with additional '/' after prefix
                remainder = path[len(path_prefix) :]
                if "/" in remainder:
                    continue
            results.append(
                FileMeta(
                    namespace_id=row[0],
                    path=row[1],
                    current_version_id=row[2],
                    current_version_number=row[3],
                    created_at=datetime.fromisoformat(row[4]),
                    updated_at=datetime.fromisoformat(row[5]),
                    is_deleted=bool(row[6]),
                )
            )
        return results

    # --- Version operations ---

    async def put_version(self, version: VersionMeta, *, expected_version: int | None = None) -> None:
        """Persist a new version; raise ConflictError before inserting if the CAS check fails."""
        now = version.created_at.isoformat()

        if expected_version is not None:
            # Pre-check before inserting to avoid orphaned version rows on CAS conflict.
            # SQLite serializes writes, so the check-then-insert is safe within a single connection.
            row = await self._execute_fetchone(
                "SELECT current_version_number FROM files WHERE namespace_id=? AND path=?",
                (version.namespace_id, version.file_path),
            )
            if row is None or row[0] != expected_version:
                raise ConflictError(
                    f"CAS conflict: expected version {expected_version} for {version.namespace_id}:{version.file_path}"
                )

        await self._db.execute(
            """
INSERT INTO versions (id, file_path, namespace_id, version_number,
    content_hash, size, created_at, created_by, is_tombstone,
    search_meta, parent_version_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
""".strip(),
            (
                version.id,
                version.file_path,
                version.namespace_id,
                version.version_number,
                version.content_hash,
                version.size,
                now,
                version.created_by,
                int(version.is_tombstone),
                json.dumps(version.search_meta),
                version.parent_version_id,
            ),
        )

        if expected_version is None:
            # New file or upsert
            await self._db.execute(
                """
INSERT INTO files (namespace_id, path, current_version_id,
    current_version_number, created_at, updated_at, is_deleted)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(namespace_id, path) DO UPDATE SET
    current_version_id=excluded.current_version_id,
    current_version_number=excluded.current_version_number,
    updated_at=excluded.updated_at,
    is_deleted=excluded.is_deleted
""".strip(),
                (
                    version.namespace_id,
                    version.file_path,
                    version.id,
                    version.version_number,
                    now,
                    now,
                    int(version.is_tombstone),
                ),
            )
        else:
            # CAS update — precondition was verified above
            await self._db.execute(
                """
UPDATE files SET current_version_id=?, current_version_number=?,
    updated_at=?, is_deleted=?
WHERE namespace_id=? AND path=? AND current_version_number=?
""".strip(),
                (
                    version.id,
                    version.version_number,
                    now,
                    int(version.is_tombstone),
                    version.namespace_id,
                    version.file_path,
                    expected_version,
                ),
            )

        await self._auto_commit()

    async def get_version(self, namespace_id: str, path: str, version_number: int | None = None) -> VersionMeta | None:
        """Return the specified version, or the latest when version_number is None."""
        if version_number is None:
            row = await self._execute_fetchone(
                """
SELECT * FROM versions
WHERE namespace_id=? AND file_path=?
ORDER BY version_number DESC LIMIT 1
""".strip(),
                (namespace_id, path),
            )
        else:
            row = await self._execute_fetchone(
                """
SELECT * FROM versions
WHERE namespace_id=? AND file_path=? AND version_number=?
""".strip(),
                (namespace_id, path, version_number),
            )
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
        if before is not None:
            rows = await self._execute_fetchall(
                """
SELECT * FROM versions
WHERE namespace_id=? AND file_path=? AND version_number < ?
ORDER BY version_number DESC LIMIT ?
""".strip(),
                (namespace_id, path, before, limit),
            )
        else:
            rows = await self._execute_fetchall(
                """
SELECT * FROM versions
WHERE namespace_id=? AND file_path=?
ORDER BY version_number DESC LIMIT ?
""".strip(),
                (namespace_id, path, limit),
            )
        return [self._row_to_version(row) for row in rows]

    # --- Permissions ---

    async def check_permission(self, principal_id: str, namespace_id: str, path: str, operation: str) -> bool:
        """Return True if the principal's most-specific matching rule allows operation."""
        rows = await self._execute_fetchall(
            "SELECT path_prefix, operations FROM permissions WHERE principal_id=? AND namespace_id=?",
            (principal_id, namespace_id),
        )
        if not rows:
            return False
        # Sort by path_prefix length descending (most-specific first)
        rows = sorted(rows, key=lambda r: len(r[0]), reverse=True)
        for row in rows:
            prefix = row[0]
            if path.startswith(prefix):
                operations = set(json.loads(row[1]))
                return operation in operations
        return False

    async def set_permission(self, permission: Permission) -> None:
        """Insert or replace the permission entry for the given (principal, namespace, path_prefix) scope."""
        await self._db.execute(
            """
INSERT INTO permissions
    (id, principal_id, namespace_id, path_prefix, operations, created_at)
VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT(principal_id, namespace_id, path_prefix) DO UPDATE SET
    id=excluded.id,
    operations=excluded.operations,
    created_at=excluded.created_at
""".strip(),
            (
                permission.id,
                permission.principal_id,
                permission.namespace_id,
                permission.path_prefix,
                json.dumps(sorted(permission.operations)),
                permission.created_at.isoformat(),
            ),
        )
        await self._auto_commit()

    async def has_any_admin(self, namespace_id: str) -> bool:
        """Return True if any permission row in the namespace lists `admin` among its operations."""
        rows = await self._execute_fetchall(
            "SELECT operations FROM permissions WHERE namespace_id=?",
            (namespace_id,),
        )
        return any("admin" in set(json.loads(row[0])) for row in rows)

    # --- Audit ---

    async def append_audit_event(self, event: AuditEvent) -> None:
        """Append an immutable audit record."""
        await self._db.execute(
            """
INSERT INTO audit_events
    (event_id, timestamp, namespace_id, principal_id, operation,
    path, version_id, detail, trace_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
""".strip(),
            (
                event.event_id,
                event.timestamp.isoformat(),
                event.namespace_id,
                event.principal_id,
                event.operation,
                event.path,
                event.version_id,
                json.dumps(event.detail),
                event.trace_id,
            ),
        )
        await self._auto_commit()

    # --- Search metadata ---

    async def update_search_meta(self, version_id: str, search_meta: dict) -> None:
        """Update the search_meta field on a version record."""
        await self._db.execute(
            "UPDATE versions SET search_meta=? WHERE id=?",
            (json.dumps(search_meta), version_id),
        )
        await self._auto_commit()

    # --- Name resolution ---

    async def set_name(self, entity_type: str, entity_id: str, display_name: str) -> None:
        """Register or replace the display name for an entity."""
        await self._db.execute(
            """
INSERT OR REPLACE INTO names (entity_type, entity_id, display_name)
VALUES (?, ?, ?)
""".strip(),
            (entity_type, entity_id, display_name),
        )
        await self._auto_commit()

    async def resolve_name(self, entity_type: str, display_name: str) -> str | None:
        """Return the entity ID for a display name, or None if not found."""
        row = await self._execute_fetchone(
            "SELECT entity_id FROM names WHERE entity_type=? AND display_name=?",
            (entity_type, display_name),
        )
        return row[0] if row else None

    # --- GC ---

    async def list_reclaimable_versions(
        self, policy: RetentionPolicy, namespace_id: str | None = None
    ) -> list[VersionMeta]:
        """Return non-tombstone versions exceeding the retention policy, excluding version 1 and the current version."""
        # Get all files in scope
        if namespace_id:
            file_rows = await self._execute_fetchall(
                "SELECT namespace_id, path FROM files WHERE namespace_id=?",
                (namespace_id,),
            )
        else:
            file_rows = await self._execute_fetchall("SELECT namespace_id, path FROM files")

        reclaimable: list[VersionMeta] = []
        for ns_id, path in file_rows:
            versions = await self._execute_fetchall(
                """
SELECT * FROM versions
WHERE namespace_id=? AND file_path=? AND is_tombstone=0
ORDER BY version_number DESC
""".strip(),
                (ns_id, path),
            )
            if len(versions) <= policy.max_recent_versions:
                continue
            # Keep the N most recent
            excess = versions[policy.max_recent_versions :]
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
        placeholders = ",".join("?" for _ in version_ids)
        await self._db.execute(
            f"DELETE FROM versions WHERE id IN ({placeholders})",  # noqa: S608 — placeholders are ? params, not user data
            tuple(version_ids),
        )
        await self._auto_commit()

    async def has_version_references(self, content_hash: str) -> bool:
        """Return True if any version record references the given content hash."""
        row = await self._execute_fetchone(
            "SELECT 1 FROM versions WHERE content_hash=? LIMIT 1",
            (content_hash,),
        )
        return row is not None

    # --- Entity persistence ---

    async def put_namespace(self, namespace: "Namespace") -> None:
        """Persist a namespace record."""
        await self._db.execute(
            """
INSERT OR REPLACE INTO namespaces (id, display_name, created_at, created_by, retention_policy)
VALUES (?, ?, ?, ?, ?)
""".strip(),
            (
                namespace.id,
                namespace.display_name,
                namespace.created_at.isoformat(),
                namespace.created_by,
                json.dumps(namespace.retention_policy.model_dump(), default=str)
                if namespace.retention_policy
                else None,
            ),
        )
        await self._auto_commit()

    async def put_principal(self, principal: "Principal") -> None:
        """Persist a principal record."""
        await self._db.execute(
            """
INSERT OR REPLACE INTO principals (id, display_name, principal_type, created_at)
VALUES (?, ?, ?, ?)
""".strip(),
            (
                principal.id,
                principal.display_name,
                principal.principal_type,
                principal.created_at.isoformat(),
            ),
        )
        await self._auto_commit()

    # --- Transactions ---

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        """Async context manager for atomic multi-step operations; rolls back on exception."""
        await self._db.execute("BEGIN")
        self._in_transaction = True
        try:
            yield
            await self._db.execute("COMMIT")
        except Exception:
            await self._db.execute("ROLLBACK")
            raise
        finally:
            self._in_transaction = False

    # --- Row mapping helpers ---

    @staticmethod
    def _row_to_version(row: tuple) -> VersionMeta:
        return VersionMeta(
            id=row[0],
            file_path=row[1],
            namespace_id=row[2],
            version_number=row[3],
            content_hash=row[4],
            size=row[5],
            created_at=datetime.fromisoformat(row[6]),
            created_by=row[7],
            is_tombstone=bool(row[8]),
            search_meta=json.loads(row[9]) if row[9] else {},
            parent_version_id=row[10],
        )
