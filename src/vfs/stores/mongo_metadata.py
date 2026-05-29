"""MongoDB-backed metadata store, implemented with Motor's async client.

Importable only when the ``mongo`` extra (``motor``) is installed; the URI resolver guards
the import and raises an actionable error otherwise. ``motor`` is imported at module scope,
so this module must not be imported unless the driver is present.

Mirrors the observable behavior of :class:`~vfs.stores.sql_metadata.BaseSqlMetadataStore`
so files and versions round-trip identically across adapters:

* ``datetime`` fields (``created_at``, ``updated_at``, ``timestamp``) are stored as ISO-8601
  strings via :meth:`datetime.isoformat` and parsed back with
  :meth:`datetime.fromisoformat`. Native BSON datetimes are deliberately *not* used because
  they lose sub-millisecond precision and tz fidelity, which would break exact equality with
  the Pydantic models and diverge from the SQL adapters.
* ``search_meta`` (versions) and ``detail`` (audit events) are stored as native subdocuments
  (plain dicts) — the explicit cross-adapter requirement for Mongo.
* ``Permission.operations`` (a set) is stored as a sorted list and read back as a ``set``.

Connection model: unlike the SQL base store, Motor's ``AsyncIOMotorClient`` is
concurrency-safe and owns its own connection pool, so this store needs **no**
``asyncio.Lock`` and **no** transaction ContextVar — concurrent coroutines can issue
operations directly against the client. The client is created lazily in :meth:`initialize`
(not in ``__init__``), so constructing the store opens no connection.

Transactions: :meth:`transaction` is a documented best-effort no-op on standalone MongoDB
(see :meth:`transaction`). Multi-document callers must order writes so partial failure is
non-destructive (see the file-operations ``MoveFile`` design); this store does not thread a
session through operations or use replica-set transactions.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
import re
from typing import Any, AsyncIterator

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError

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

#: Database name used when the URI path is empty (e.g. ``mongodb://localhost``).
_DEFAULT_DB_NAME = "aifs"


class MongoMetadataStore:
    """MetadataStore implementation backed by MongoDB via Motor.

    See the module docstring for the connection model, datetime/subdocument storage rules,
    and the best-effort ``transaction()`` contract.
    """

    def __init__(self, uri: str) -> None:
        """Store the URI; open no connection.

        The resolver passes the full URI (e.g. ``mongodb://host/db``). The client and the
        resolved database are created in :meth:`initialize`, not here — the URI-resolution
        unit test asserts no connection is opened at construction. The database name carried
        in the URI path is resolved from the client in :meth:`initialize` via the stable
        public ``get_default_database`` API (defaulting to ``"aifs"`` when the path is empty),
        which avoids depending on the deprecated ``pymongo.uri_parser`` internals.
        """
        self._uri = uri
        self._client: AsyncIOMotorClient | None = None
        self._database: AsyncIOMotorDatabase | None = None

    # --- Lifecycle ---

    async def initialize(self) -> None:
        """Create the Motor client, resolve the database, and create indexes."""
        self._client = AsyncIOMotorClient(self._uri)
        # get_default_database reads the db from the URI path; fall back to _DEFAULT_DB_NAME.
        self._database = self._client.get_default_database(default=_DEFAULT_DB_NAME)
        db = self._db
        await db.files.create_index([("namespace_id", 1), ("path", 1)], unique=True)
        await db.versions.create_index([("namespace_id", 1), ("file_path", 1), ("version_number", 1)], unique=True)
        await db.versions.create_index([("namespace_id", 1), ("file_path", 1), ("version_number", -1)])
        await db.versions.create_index([("content_hash", 1)])
        await db.permissions.create_index([("principal_id", 1), ("namespace_id", 1), ("path_prefix", 1)], unique=True)
        await db.names.create_index([("entity_type", 1), ("entity_id", 1)], unique=True)
        await db.names.create_index([("entity_type", 1), ("display_name", 1)], unique=True)
        # Enforce the domain primary keys the SQL schema declares (see vfs.stores.schema):
        # each entity's id is unique within its collection.
        await db.versions.create_index([("id", 1)], unique=True)
        await db.audit_events.create_index([("event_id", 1)], unique=True)
        await db.namespaces.create_index([("id", 1)], unique=True)
        await db.principals.create_index([("id", 1)], unique=True)
        await db.permissions.create_index([("id", 1)], unique=True)

    async def close(self) -> None:
        """Close the Motor client; safe to call when never initialized."""
        if self._client is not None:
            self._client.close()
            self._client = None
            self._database = None

    @property
    def _db(self) -> AsyncIOMotorDatabase:
        if self._database is None:
            raise RuntimeError(f"{type(self).__name__} is not initialized; call initialize() first")
        return self._database

    # --- File operations ---

    async def put_file(self, file_meta: FileMeta) -> None:
        """Insert or replace a file record, keyed by (namespace_id, path).

        On update only the mutable pointer/state fields are overwritten; ``created_at`` is
        preserved via ``$setOnInsert`` so re-putting an existing path keeps its original
        creation time. This mirrors the SQL base store, whose upsert ``set_`` list excludes
        ``created_at``.
        """
        await self._db.files.update_one(
            {"namespace_id": file_meta.namespace_id, "path": file_meta.path},
            {
                "$set": {
                    "current_version_id": file_meta.current_version_id,
                    "current_version_number": file_meta.current_version_number,
                    "updated_at": file_meta.updated_at.isoformat(),
                    "is_deleted": file_meta.is_deleted,
                },
                "$setOnInsert": {
                    "namespace_id": file_meta.namespace_id,
                    "path": file_meta.path,
                    "created_at": file_meta.created_at.isoformat(),
                },
            },
            upsert=True,
        )

    async def get_file(self, namespace_id: str, path: str) -> FileMeta | None:
        """Return the file record for namespace_id/path, or None if absent."""
        doc = await self._db.files.find_one({"namespace_id": namespace_id, "path": path})
        if doc is None:
            return None
        return self._doc_to_file(doc)

    async def delete_file(self, namespace_id: str, path: str) -> None:
        """Hard-delete the file record (use put_version with is_tombstone for soft delete)."""
        await self._db.files.delete_one({"namespace_id": namespace_id, "path": path})

    async def list_dir(self, namespace_id: str, path_prefix: str, *, recursive: bool = False) -> list[FileMeta]:
        """List live (non-deleted) files under path_prefix; recurse into subdirectories when recursive=True."""
        cursor = self._db.files.find(
            {
                "namespace_id": namespace_id,
                "is_deleted": False,
                "path": {"$regex": f"^{re.escape(path_prefix)}"},
            }
        )
        results: list[FileMeta] = []
        async for doc in cursor:
            path = doc["path"]
            if not recursive:
                # Non-recursive: exclude paths with an additional '/' after the prefix.
                remainder = path[len(path_prefix) :]
                if "/" in remainder:
                    continue
            results.append(self._doc_to_file(doc))
        return results

    # --- Version operations ---

    async def put_version(self, version: VersionMeta, *, expected_version: int | None = None) -> None:
        """Persist a new version and advance the file's current-version pointer.

        When ``expected_version`` is set, the version document is INSERTED FIRST, then the
        pointer is advanced with an atomic ``find_one_and_update`` filtered by
        ``current_version_number == expected_version``. Insert-before-pointer ordering matters
        because there is no transaction to roll back on standalone MongoDB: were the pointer
        advanced first and the insert then failed (e.g. the unique
        ``(namespace_id, file_path, version_number)`` index, or a transient error), the
        pointer would reference a version that was never inserted. With insert-first the
        version always exists before the pointer moves. If the CAS matches no file document —
        the file was concurrently advanced, or does not exist — the just-inserted version is
        deleted and :class:`~vfs.errors.ConflictError` is raised, preserving the
        no-orphan-on-conflict contract.

        Between the insert and the pointer advance a concurrent latest-version read may
        briefly observe the new version before it is pointed-to (or, on conflict, before it
        is removed). This is an accepted best-effort-store tradeoff — the spec declares
        standalone MongoDB non-atomic for multi-document writes — not present on the SQL
        adapters, which serialize on a single connection.
        """
        version_doc = self._version_to_doc(version)
        pointer = {
            "current_version_id": version.id,
            "current_version_number": version.version_number,
            "updated_at": version.created_at.isoformat(),
            "is_deleted": version.is_tombstone,
        }

        if expected_version is None:
            # New file or unconditional upsert: insert the version, then upsert the pointer.
            await self._db.versions.insert_one(version_doc)
            now = version.created_at.isoformat()
            await self._db.files.update_one(
                {"namespace_id": version.namespace_id, "path": version.file_path},
                {
                    "$set": {**pointer, "updated_at": now},
                    "$setOnInsert": {
                        "namespace_id": version.namespace_id,
                        "path": version.file_path,
                        "created_at": now,
                    },
                },
                upsert=True,
            )
        else:
            # Insert the version first so the pointer can never reference a missing version
            # (there is no transaction to roll back a failed insert on standalone MongoDB).
            await self._db.versions.insert_one(version_doc)
            # Compare-and-swap: advance the pointer only while the file is still at
            # expected_version. No matching document => conflict.
            updated = await self._db.files.find_one_and_update(
                {
                    "namespace_id": version.namespace_id,
                    "path": version.file_path,
                    "current_version_number": expected_version,
                },
                {"$set": pointer},
            )
            if updated is None:
                # CAS conflict: remove the version we just inserted so no orphan remains,
                # matching the SQL adapter's no-orphan-on-conflict contract.
                await self._db.versions.delete_one({"id": version.id})
                raise ConflictError(
                    f"CAS conflict: expected version {expected_version} for {version.namespace_id}:{version.file_path}"
                )

    async def get_version(self, namespace_id: str, path: str, version_number: int | None = None) -> VersionMeta | None:
        """Return the specified version, or the latest when version_number is None."""
        if version_number is None:
            doc = await self._db.versions.find_one(
                {"namespace_id": namespace_id, "file_path": path},
                sort=[("version_number", -1)],
            )
        else:
            doc = await self._db.versions.find_one(
                {"namespace_id": namespace_id, "file_path": path, "version_number": version_number}
            )
        if doc is None:
            return None
        return self._doc_to_version(doc)

    async def list_versions(
        self,
        namespace_id: str,
        path: str,
        *,
        limit: int = 50,
        before: int | None = None,
    ) -> list[VersionMeta]:
        """Return up to limit versions, newest-first; cursor-paginate with before (exclusive)."""
        query: dict[str, Any] = {"namespace_id": namespace_id, "file_path": path}
        if before is not None:
            query["version_number"] = {"$lt": before}
        cursor = self._db.versions.find(query).sort("version_number", -1).limit(limit)
        return [self._doc_to_version(doc) async for doc in cursor]

    # --- Permissions ---

    async def check_permission(self, principal_id: str, namespace_id: str, path: str, operation: str) -> bool:
        """Return True if the principal's most-specific matching rule allows operation."""
        cursor = self._db.permissions.find({"principal_id": principal_id, "namespace_id": namespace_id})
        rows = [doc async for doc in cursor]
        if not rows:
            return False
        # Most-specific (longest prefix) first.
        rows.sort(key=lambda r: len(r["path_prefix"]), reverse=True)
        for row in rows:
            if path.startswith(row["path_prefix"]):
                return operation in set(row["operations"])
        return False

    async def set_permission(self, permission: Permission) -> None:
        """Insert or replace the permission entry for the (principal, namespace, path_prefix) scope."""
        await self._db.permissions.update_one(
            {
                "principal_id": permission.principal_id,
                "namespace_id": permission.namespace_id,
                "path_prefix": permission.path_prefix,
            },
            {
                "$set": {
                    "id": permission.id,
                    "principal_id": permission.principal_id,
                    "namespace_id": permission.namespace_id,
                    "path_prefix": permission.path_prefix,
                    "operations": sorted(permission.operations),
                    "created_at": permission.created_at.isoformat(),
                }
            },
            upsert=True,
        )

    async def has_any_admin(self, namespace_id: str) -> bool:
        """Return True if any permission in the namespace lists `admin` among its operations."""
        doc = await self._db.permissions.find_one({"namespace_id": namespace_id, "operations": "admin"})
        return doc is not None

    # --- Audit ---

    async def append_audit_event(self, event: AuditEvent) -> None:
        """Append an immutable audit record."""
        await self._db.audit_events.insert_one(
            {
                "event_id": event.event_id,
                "timestamp": event.timestamp.isoformat(),
                "namespace_id": event.namespace_id,
                "principal_id": event.principal_id,
                "operation": event.operation,
                "path": event.path,
                "version_id": event.version_id,
                "detail": event.detail,
                "trace_id": event.trace_id,
            }
        )

    # --- Search metadata ---

    async def update_search_meta(self, version_id: str, search_meta: dict) -> None:
        """Update the search_meta field on a version record."""
        await self._db.versions.update_one({"id": version_id}, {"$set": {"search_meta": search_meta}})

    # --- Name resolution ---

    async def set_name(self, entity_type: str, entity_id: str, display_name: str) -> None:
        """Register or replace the display name for an entity.

        Registering or renaming the *same* entity updates its display name (the upsert is
        keyed by ``(entity_type, entity_id)``). Claiming a ``display_name`` already held by a
        *different* entity of the same ``entity_type`` violates the unique
        ``(entity_type, display_name)`` index; the resulting
        :class:`~pymongo.errors.DuplicateKeyError` is translated to
        :class:`~vfs.errors.ConflictError`. There is no transaction to poison on standalone
        MongoDB, so the store stays usable after the rejected write.
        """
        try:
            await self._db.names.update_one(
                {"entity_type": entity_type, "entity_id": entity_id},
                {"$set": {"display_name": display_name}},
                upsert=True,
            )
        except DuplicateKeyError as exc:
            raise ConflictError(
                f"display name {display_name!r} is already in use for entity_type {entity_type!r}"
            ) from exc

    async def resolve_name(self, entity_type: str, display_name: str) -> str | None:
        """Return the entity ID for a display name, or None if not found."""
        doc = await self._db.names.find_one({"entity_type": entity_type, "display_name": display_name})
        return doc["entity_id"] if doc else None

    # --- GC ---

    async def list_reclaimable_versions(
        self, policy: RetentionPolicy, namespace_id: str | None = None
    ) -> list[VersionMeta]:
        """Return non-tombstone versions exceeding the retention policy, excluding version 1 when configured."""
        file_query: dict[str, Any] = {}
        if namespace_id:
            file_query["namespace_id"] = namespace_id

        reclaimable: list[VersionMeta] = []
        async for file_doc in self._db.files.find(file_query, projection=["namespace_id", "path"]):
            version_cursor = self._db.versions.find(
                {
                    "namespace_id": file_doc["namespace_id"],
                    "file_path": file_doc["path"],
                    "is_tombstone": False,
                }
            ).sort("version_number", -1)
            version_docs = [doc async for doc in version_cursor]
            if len(version_docs) <= policy.max_recent_versions:
                continue
            # Keep the N most recent; the excess is candidate for reclamation.
            excess = version_docs[policy.max_recent_versions :]
            for doc in excess:
                ver = self._doc_to_version(doc)
                if policy.keep_first_version and ver.version_number == 1:
                    continue
                reclaimable.append(ver)
        return reclaimable

    async def delete_versions(self, version_ids: list[str]) -> None:
        """Hard-delete version records by ID."""
        if not version_ids:
            return
        await self._db.versions.delete_many({"id": {"$in": version_ids}})

    async def has_version_references(self, content_hash: str) -> bool:
        """Return True if any version record references the given content hash."""
        doc = await self._db.versions.find_one({"content_hash": content_hash})
        return doc is not None

    # --- Entity persistence ---

    async def put_namespace(self, namespace: Namespace) -> None:
        """Persist a namespace record, keyed by id."""
        retention = namespace.retention_policy.model_dump() if namespace.retention_policy else None
        await self._db.namespaces.update_one(
            {"id": namespace.id},
            {
                "$set": {
                    "id": namespace.id,
                    "display_name": namespace.display_name,
                    "created_at": namespace.created_at.isoformat(),
                    "created_by": namespace.created_by,
                    "retention_policy": retention,
                }
            },
            upsert=True,
        )

    async def put_principal(self, principal: Principal) -> None:
        """Persist a principal record, keyed by id."""
        await self._db.principals.update_one(
            {"id": principal.id},
            {
                "$set": {
                    "id": principal.id,
                    "display_name": principal.display_name,
                    "principal_type": principal.principal_type,
                    "created_at": principal.created_at.isoformat(),
                }
            },
            upsert=True,
        )

    # --- Transactions ---

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        """Best-effort no-op transaction on standalone MongoDB.

        MongoDB provides true atomicity only on replica-set deployments; on standalone
        MongoDB this yields without opening a session, so multi-document writes are not
        rolled back as a unit. Callers performing multi-document mutations MUST NOT rely on
        rollback for non-destructiveness; they MUST order writes so partial failure is
        non-destructive (see the file-operations ``MoveFile`` design). This is the
        deliberate Phase 2 design decision — no session is threaded through operations.
        """
        yield

    # --- Document mapping helpers ---

    @staticmethod
    def _doc_to_file(doc: dict[str, Any]) -> FileMeta:
        return FileMeta(
            namespace_id=doc["namespace_id"],
            path=doc["path"],
            current_version_id=doc["current_version_id"],
            current_version_number=doc["current_version_number"],
            created_at=datetime.fromisoformat(doc["created_at"]),
            updated_at=datetime.fromisoformat(doc["updated_at"]),
            is_deleted=bool(doc["is_deleted"]),
        )

    @staticmethod
    def _version_to_doc(version: VersionMeta) -> dict[str, Any]:
        return {
            "id": version.id,
            "file_path": version.file_path,
            "namespace_id": version.namespace_id,
            "version_number": version.version_number,
            "content_hash": version.content_hash,
            "size": version.size,
            "created_at": version.created_at.isoformat(),
            "created_by": version.created_by,
            "is_tombstone": version.is_tombstone,
            "search_meta": version.search_meta,
            "parent_version_id": version.parent_version_id,
        }

    @staticmethod
    def _doc_to_version(doc: dict[str, Any]) -> VersionMeta:
        return VersionMeta(
            id=doc["id"],
            file_path=doc["file_path"],
            namespace_id=doc["namespace_id"],
            version_number=doc["version_number"],
            content_hash=doc["content_hash"],
            size=doc["size"],
            created_at=datetime.fromisoformat(doc["created_at"]),
            created_by=doc["created_by"],
            is_tombstone=bool(doc["is_tombstone"]),
            search_meta=doc["search_meta"] if doc.get("search_meta") else {},
            parent_version_id=doc.get("parent_version_id"),
        )
