"""VFS orchestrator — the main entry point for file system operations."""

from __future__ import annotations

from datetime import datetime, timezone
import importlib
import importlib.util
import time

import blake3
from ulid import ULID

from vfs.config import VFSConfig
from vfs.errors import NotFoundError, PermissionDeniedError
from vfs.models import (
    AuditEvent,
    FileMeta,
    Namespace,
    Permission,
    Principal,
    SearchResult,
    SearchType,
    VersionMeta,
)
from vfs.observability.audit import (
    audit,
    audit_copy,
    audit_delete,
    audit_move,
    audit_permission_change,
    audit_rollback,
    audit_write,
)
from vfs.observability.tracing import (
    record_blob_size,
    record_op,
    record_search_candidates,
    vfs_span,
)
from vfs.search.default import DefaultSearchProvider
from vfs.stores.cached_blob import CachedBlobStore
from vfs.stores.local_blob import LocalFSBlobStore
from vfs.stores.sqlite_metadata import SQLiteMetadataStore

#: Built-in adapters with no optional dependency, mapping URI scheme to class.
_METADATA_SCHEMES = {
    "sqlite:///": SQLiteMetadataStore,
}
_BLOB_SCHEMES = {
    "file:///": LocalFSBlobStore,
}

#: Adapters resolved lazily because they require an installable extra.
#: scheme -> (extra_name, driver_module, adapter_module, class_name).
#: Driver-backed adapters receive the full URI (asyncpg/motor/aiobotocore parse it themselves).
_METADATA_OPTIONAL = {
    "postgresql://": ("postgres", "asyncpg", "vfs.stores.postgres_metadata", "PostgresMetadataStore"),
    "mongodb://": ("mongo", "motor", "vfs.stores.mongo_metadata", "MongoMetadataStore"),
}
_BLOB_OPTIONAL = {
    "s3://": ("s3", "aiobotocore", "vfs.stores.s3_blob", "S3BlobStore"),
}


def _load_optional_adapter(scheme: str, spec: tuple[str, str, str, str]) -> type:
    """Import and return an optional adapter class, or raise a clear, actionable error.

    Both the driver and the adapter module are probed before import so neither a missing
    optional dependency nor a not-yet-shipped adapter surfaces as an opaque
    ``ModuleNotFoundError``.
    """
    extra, driver, adapter_module, class_name = spec
    if importlib.util.find_spec(driver) is None:
        raise ImportError(
            f"{scheme!r} support requires the optional {extra!r} extra "
            f"(missing dependency {driver!r}). Install it with: pip install 'ai-vfs[{extra}]'"
        )
    if importlib.util.find_spec(adapter_module) is None:
        raise ImportError(
            f"{scheme!r} support is not available in this build of ai-vfs (adapter {adapter_module!r} is not present)."
        )
    module = importlib.import_module(adapter_module)
    return getattr(module, class_name)


def _require_absolute(path: str) -> None:
    """Reject any path that is not absolute. Relative-path resolution is the caller's job."""
    if not path.startswith("/"):
        raise ValueError(f"path must be absolute, got {path!r}")


class VFS:
    """Virtual file system orchestrator."""

    def __init__(self, config: VFSConfig | None = None) -> None:
        self._config = config or VFSConfig()
        self._meta = self._resolve_metadata_store()
        self._blob = self._resolve_blob_store()
        self._search = DefaultSearchProvider()

    def _resolve_metadata_store(self):
        uri = self._config.metadata_store_uri
        for scheme, cls in _METADATA_SCHEMES.items():
            if uri.startswith(scheme):
                return cls(uri[len(scheme) :])
        for scheme, spec in _METADATA_OPTIONAL.items():
            if uri.startswith(scheme):
                return _load_optional_adapter(scheme, spec)(uri)
        supported = ", ".join([*_METADATA_SCHEMES, *_METADATA_OPTIONAL])
        raise ValueError(f"Unsupported metadata URI {uri!r}. Supported: {supported}")

    def _resolve_blob_store(self):
        uri = self._config.blob_store_uri
        for scheme, cls in _BLOB_SCHEMES.items():
            if uri.startswith(scheme):
                return self._maybe_wrap_cache(cls(uri[len(scheme) :]), uri)
        for scheme, spec in _BLOB_OPTIONAL.items():
            if uri.startswith(scheme):
                store = _load_optional_adapter(scheme, spec)(uri)
                return self._maybe_wrap_cache(store, uri)
        supported = ", ".join([*_BLOB_SCHEMES, *_BLOB_OPTIONAL])
        raise ValueError(f"Unsupported blob URI {uri!r}. Supported: {supported}")

    def _maybe_wrap_cache(self, store, uri: str):
        cache_enabled = self._config.blob_cache_enabled
        if cache_enabled is None:
            # Auto: disable for local FS
            if uri.startswith("file:///"):
                return store
            cache_enabled = True
        if not cache_enabled:
            return store
        import tempfile

        cache_dir = self._config.blob_cache_dir or tempfile.mkdtemp(prefix="vfs-cache-")
        return CachedBlobStore(store, cache_dir, self._config.blob_cache_max_size_mb)

    async def initialize(self, *, set_proc_title: bool = False) -> None:
        """Initialize storage backends and optionally set the process title."""
        await self._meta.initialize()
        if set_proc_title:
            import setproctitle

            setproctitle.setproctitle("ai-vfs: service")

    async def close(self) -> None:
        """Close storage connections."""
        await self._meta.close()
        if isinstance(self._blob, CachedBlobStore):
            self._blob.close()

    # --- Permission helper ---

    async def _check_perm(self, principal_id: str, namespace_id: str, path: str, operation: str) -> None:
        if not await self._meta.check_permission(principal_id, namespace_id, path, operation):
            raise PermissionDeniedError(f"Principal {principal_id!r} lacks {operation!r} on {namespace_id}:{path}")

    # --- stat / list ---

    async def stat(self, namespace_id: str, path: str, *, principal_id: str) -> FileMeta:
        """Return file metadata. Raises PermissionDeniedError or NotFoundError."""
        _require_absolute(path)
        t0 = time.monotonic()
        with vfs_span(
            "stat",
            {"vfs.namespace": namespace_id, "vfs.path": path, "vfs.principal_id": principal_id},
            otel_enabled=self._config.otel_enabled,
        ):
            await self._check_perm(principal_id, namespace_id, path, "read")
            meta = await self._meta.get_file(namespace_id, path)
            if meta is None:
                raise NotFoundError(f"File not found: {namespace_id}:{path}")
            record_op(
                "stat",
                (time.monotonic() - t0) * 1000,
                {"vfs.namespace": namespace_id},
                otel_enabled=self._config.otel_enabled,
            )
            return meta

    async def list(
        self,
        namespace_id: str,
        path_prefix: str,
        *,
        principal_id: str,
        recursive: bool = False,
    ) -> list[FileMeta]:
        """List files under path_prefix, silently pruning entries the principal cannot read."""
        _require_absolute(path_prefix)
        t0 = time.monotonic()
        with vfs_span(
            "list",
            {"vfs.namespace": namespace_id, "vfs.path": path_prefix, "vfs.principal_id": principal_id},
            otel_enabled=self._config.otel_enabled,
        ):
            files = await self._meta.list_dir(namespace_id, path_prefix, recursive=recursive)
            # Invisible pruning: filter out files the principal cannot read
            result = []
            for f in files:
                if await self._meta.check_permission(principal_id, namespace_id, f.path, "read"):
                    result.append(f)
            record_op(
                "list",
                (time.monotonic() - t0) * 1000,
                {"vfs.namespace": namespace_id},
                otel_enabled=self._config.otel_enabled,
            )
            return result

    # --- write ---

    async def write(
        self,
        namespace_id: str,
        path: str,
        content: bytes,
        *,
        principal_id: str,
        expected_version: int | None = None,
    ) -> VersionMeta:
        """Write content and create a new immutable version. Raises ConflictError on CAS mismatch."""
        _require_absolute(path)
        await self._check_perm(principal_id, namespace_id, path, "write")
        t0 = time.monotonic()

        with vfs_span(
            "write",
            {"vfs.namespace": namespace_id, "vfs.path": path, "vfs.principal_id": principal_id},
            otel_enabled=self._config.otel_enabled,
        ):
            content_hash = blake3.blake3(content).hexdigest()
            await self._blob.put(content_hash, content)
            record_blob_size(len(content), otel_enabled=self._config.otel_enabled)

            # Determine version number
            existing = await self._meta.get_file(namespace_id, path)
            version_number = 1 if existing is None else existing.current_version_number + 1

            # Index for search
            file_meta = existing or FileMeta(
                namespace_id=namespace_id,
                path=path,
                current_version_id="",
                current_version_number=0,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            search_meta = await self._search.index(path, content, file_meta)

            now = datetime.now(timezone.utc)
            version = VersionMeta(
                id=str(ULID()),
                file_path=path,
                namespace_id=namespace_id,
                version_number=version_number,
                content_hash=content_hash,
                size=len(content),
                created_at=now,
                created_by=principal_id,
                search_meta=search_meta,
            )

            await self._meta.put_version(version, expected_version=expected_version)

            await audit_write(
                self._meta,
                namespace_id=namespace_id,
                principal_id=principal_id,
                path=path,
                version_id=version.id,
                audit_log_enabled=self._config.audit_log_enabled,
            )

            duration_ms = (time.monotonic() - t0) * 1000
            record_op(
                "write",
                duration_ms,
                {"vfs.namespace": namespace_id},
                otel_enabled=self._config.otel_enabled,
            )

        return version

    # --- read ---

    async def read(
        self,
        namespace_id: str,
        path: str,
        *,
        principal_id: str,
        version_number: int | None = None,
    ) -> bytes:
        """Return blob content for the current version, or a specific version if version_number is given."""
        _require_absolute(path)
        t0 = time.monotonic()
        with vfs_span(
            "read",
            {"vfs.namespace": namespace_id, "vfs.path": path, "vfs.principal_id": principal_id},
            otel_enabled=self._config.otel_enabled,
        ):
            await self._check_perm(principal_id, namespace_id, path, "read")
            ver = await self._meta.get_version(namespace_id, path, version_number)
            if ver is None or ver.is_tombstone:
                raise NotFoundError(f"File not found: {namespace_id}:{path}")
            data = await self._blob.get(ver.content_hash)
            record_op(
                "read",
                (time.monotonic() - t0) * 1000,
                {"vfs.namespace": namespace_id},
                otel_enabled=self._config.otel_enabled,
            )
            return data

    # --- delete ---

    async def delete(self, namespace_id: str, path: str, *, principal_id: str) -> VersionMeta:
        """Create a tombstone version marking the file deleted; preserves prior versions."""
        _require_absolute(path)
        t0 = time.monotonic()
        with vfs_span(
            "delete",
            {"vfs.namespace": namespace_id, "vfs.path": path, "vfs.principal_id": principal_id},
            otel_enabled=self._config.otel_enabled,
        ):
            await self._check_perm(principal_id, namespace_id, path, "delete")
            existing = await self._meta.get_file(namespace_id, path)
            if existing is None:
                raise NotFoundError(f"File not found: {namespace_id}:{path}")

            now = datetime.now(timezone.utc)
            tombstone = VersionMeta(
                id=str(ULID()),
                file_path=path,
                namespace_id=namespace_id,
                version_number=existing.current_version_number + 1,
                content_hash="",
                size=0,
                created_at=now,
                created_by=principal_id,
                is_tombstone=True,
            )
            await self._meta.put_version(tombstone)

            await audit_delete(
                self._meta,
                namespace_id=namespace_id,
                principal_id=principal_id,
                path=path,
                version_id=tombstone.id,
                audit_log_enabled=self._config.audit_log_enabled,
            )
            record_op(
                "delete",
                (time.monotonic() - t0) * 1000,
                {"vfs.namespace": namespace_id},
                otel_enabled=self._config.otel_enabled,
            )
            return tombstone

    # --- copy ---

    async def copy(
        self,
        namespace_id: str,
        src: str,
        dst: str,
        *,
        principal_id: str,
        expected_version: int | None = None,
    ) -> VersionMeta:
        """Copy src to dst within the same namespace, sharing the underlying blob."""
        _require_absolute(src)
        _require_absolute(dst)
        t0 = time.monotonic()
        with vfs_span(
            "copy",
            {"vfs.namespace": namespace_id, "vfs.path": f"{src}->{dst}", "vfs.principal_id": principal_id},
            otel_enabled=self._config.otel_enabled,
        ):
            await self._check_perm(principal_id, namespace_id, src, "read")
            await self._check_perm(principal_id, namespace_id, dst, "write")

            src_version = await self._meta.get_version(namespace_id, src)
            if src_version is None or src_version.is_tombstone:
                raise NotFoundError(f"Source not found: {namespace_id}:{src}")

            dst_file = await self._meta.get_file(namespace_id, dst)
            dst_version_number = (dst_file.current_version_number + 1) if dst_file else 1

            now = datetime.now(timezone.utc)
            new_version = VersionMeta(
                id=str(ULID()),
                file_path=dst,
                namespace_id=namespace_id,
                version_number=dst_version_number,
                content_hash=src_version.content_hash,
                size=src_version.size,
                created_at=now,
                created_by=principal_id,
            )
            await self._meta.put_version(new_version, expected_version=expected_version)

            await audit_copy(
                self._meta,
                namespace_id=namespace_id,
                principal_id=principal_id,
                src_path=src,
                dst_path=dst,
                version_id=new_version.id,
                audit_log_enabled=self._config.audit_log_enabled,
            )
            record_op(
                "copy",
                (time.monotonic() - t0) * 1000,
                {"vfs.namespace": namespace_id},
                otel_enabled=self._config.otel_enabled,
            )
            return new_version

    # --- move ---

    async def move(
        self,
        namespace_id: str,
        src: str,
        dst: str,
        *,
        principal_id: str,
    ) -> VersionMeta:
        """Atomically tombstone src and create dst with the same content hash."""
        _require_absolute(src)
        _require_absolute(dst)
        t0 = time.monotonic()
        with vfs_span(
            "move",
            {"vfs.namespace": namespace_id, "vfs.path": f"{src}->{dst}", "vfs.principal_id": principal_id},
            otel_enabled=self._config.otel_enabled,
        ):
            await self._check_perm(principal_id, namespace_id, src, "read")
            await self._check_perm(principal_id, namespace_id, src, "delete")
            await self._check_perm(principal_id, namespace_id, dst, "write")

            src_version = await self._meta.get_version(namespace_id, src)
            if src_version is None or src_version.is_tombstone:
                raise NotFoundError(f"Source not found: {namespace_id}:{src}")

            src_file = await self._meta.get_file(namespace_id, src)
            dst_file = await self._meta.get_file(namespace_id, dst)
            dst_version_number = (dst_file.current_version_number + 1) if dst_file else 1

            now = datetime.now(timezone.utc)

            async with self._meta.transaction():
                # Create at destination FIRST. On a best-effort no-op transaction()
                # (standalone Mongo) each put_version commits on its own, so writing the
                # destination before the source tombstone makes a mid-move failure
                # non-destructive (a duplicate, never a loss) per the MoveFile spec; on a
                # transactional store the whole block still rolls back atomically.
                new_version = VersionMeta(
                    id=str(ULID()),
                    file_path=dst,
                    namespace_id=namespace_id,
                    version_number=dst_version_number,
                    content_hash=src_version.content_hash,
                    size=src_version.size,
                    created_at=now,
                    created_by=principal_id,
                )
                await self._meta.put_version(new_version)

                # Tombstone source second.
                tombstone = VersionMeta(
                    id=str(ULID()),
                    file_path=src,
                    namespace_id=namespace_id,
                    version_number=src_file.current_version_number + 1,
                    content_hash="",
                    size=0,
                    created_at=now,
                    created_by=principal_id,
                    is_tombstone=True,
                )
                await self._meta.put_version(tombstone)

            await audit_move(
                self._meta,
                namespace_id=namespace_id,
                principal_id=principal_id,
                src_path=src,
                dst_path=dst,
                version_id=new_version.id,
                audit_log_enabled=self._config.audit_log_enabled,
            )
            record_op(
                "move",
                (time.monotonic() - t0) * 1000,
                {"vfs.namespace": namespace_id},
                otel_enabled=self._config.otel_enabled,
            )
            return new_version

    # --- versions / rollback ---

    async def versions(
        self,
        namespace_id: str,
        path: str,
        *,
        principal_id: str,
        limit: int = 50,
        before: int | None = None,
    ) -> list[VersionMeta]:
        """Return version history for path, newest-first."""
        _require_absolute(path)
        t0 = time.monotonic()
        with vfs_span(
            "versions",
            {"vfs.namespace": namespace_id, "vfs.path": path, "vfs.principal_id": principal_id},
            otel_enabled=self._config.otel_enabled,
        ):
            await self._check_perm(principal_id, namespace_id, path, "read")
            result = await self._meta.list_versions(namespace_id, path, limit=limit, before=before)
            record_op(
                "versions",
                (time.monotonic() - t0) * 1000,
                {"vfs.namespace": namespace_id},
                otel_enabled=self._config.otel_enabled,
            )
            return result

    async def rollback(
        self,
        namespace_id: str,
        path: str,
        target_version: int,
        *,
        principal_id: str,
    ) -> VersionMeta:
        """Create a new version restoring target_version's content."""
        _require_absolute(path)
        t0 = time.monotonic()
        with vfs_span(
            "rollback",
            {"vfs.namespace": namespace_id, "vfs.path": path, "vfs.principal_id": principal_id},
            otel_enabled=self._config.otel_enabled,
        ):
            await self._check_perm(principal_id, namespace_id, path, "write")
            target = await self._meta.get_version(namespace_id, path, target_version)
            if target is None:
                raise NotFoundError(f"Version {target_version} not found for {namespace_id}:{path}")

            existing = await self._meta.get_file(namespace_id, path)
            next_num = (existing.current_version_number + 1) if existing else 1

            now = datetime.now(timezone.utc)
            new_version = VersionMeta(
                id=str(ULID()),
                file_path=path,
                namespace_id=namespace_id,
                version_number=next_num,
                content_hash=target.content_hash,
                size=target.size,
                created_at=now,
                created_by=principal_id,
                parent_version_id=target.id,
            )
            await self._meta.put_version(new_version)

            await audit_rollback(
                self._meta,
                namespace_id=namespace_id,
                principal_id=principal_id,
                path=path,
                version_id=new_version.id,
                target_version_id=target.id,
                audit_log_enabled=self._config.audit_log_enabled,
            )
            record_op(
                "rollback",
                (time.monotonic() - t0) * 1000,
                {"vfs.namespace": namespace_id},
                otel_enabled=self._config.otel_enabled,
            )
            return new_version

    # --- search ---

    async def search(
        self,
        namespace_id: str,
        query: str,
        scope: str,
        search_type: SearchType,
        *,
        principal_id: str,
    ) -> list[SearchResult]:
        """Search files in scope, silently pruning results the principal cannot read."""
        _require_absolute(scope)
        t0 = time.monotonic()
        with vfs_span(
            "search",
            {"vfs.namespace": namespace_id, "vfs.path": scope, "vfs.principal_id": principal_id},
            otel_enabled=self._config.otel_enabled,
        ):
            # Capability validation
            if search_type not in self._search.capabilities():
                raise ValueError(
                    f"No search provider supports {search_type.value!r}. "
                    f"Available: {sorted(t.value for t in self._search.capabilities())}"
                )

            # List all files in scope
            all_files = await self._meta.list_dir(namespace_id, scope, recursive=True)
            # Invisible pruning
            candidates = [
                f for f in all_files if await self._meta.check_permission(principal_id, namespace_id, f.path, "read")
            ]
            record_search_candidates(
                len(candidates),
                {"vfs.namespace": namespace_id, "vfs.search_type": search_type.value},
                otel_enabled=self._config.otel_enabled,
            )

            # Content fetcher closure — provider calls on demand
            async def _fetch_content(path: str) -> bytes:
                ver = await self._meta.get_version(namespace_id, path)
                if ver is None or ver.is_tombstone:
                    return b""
                return await self._blob.get(ver.content_hash)

            results = await self._search.search(query, scope, search_type, candidates, _fetch_content)
            record_op(
                "search",
                (time.monotonic() - t0) * 1000,
                {"vfs.namespace": namespace_id},
                otel_enabled=self._config.otel_enabled,
            )
            return results

    # --- GC / reindex ---

    async def run_gc(self, namespace_id: str | None = None):
        """Run garbage collection and return a GCResult."""
        from vfs.gc import GarbageCollector

        gc = GarbageCollector(self._meta, self._blob, self._config)
        return await gc.run(namespace_id)

    async def reindex(self, namespace_id: str, provider_name: str = "default", scope: str = "/") -> int:
        """Backfill search metadata for files in scope; returns the count of versions updated."""
        _require_absolute(scope)
        files = await self._meta.list_dir(namespace_id, scope, recursive=True)
        count = 0
        for f in files:
            ver = await self._meta.get_version(namespace_id, f.path)
            if ver and not ver.is_tombstone:
                content = await self._blob.get(ver.content_hash)
                search_meta = await self._search.index(f.path, content, f)
                if search_meta:
                    await self._meta.update_search_meta(ver.id, search_meta)
                    count += 1
        return count

    # --- Namespace / permission helpers ---

    async def grant(
        self,
        granter_id: str,
        target_principal_id: str,
        namespace_id: str,
        path_prefix: str,
        operations: set[str],
    ) -> None:
        """Grant `target_principal_id` the given operations on `path_prefix`.

        The `granter_id` principal MUST hold the `admin` operation on
        `path_prefix` (or a less-specific prefix that covers it) within the
        namespace; otherwise `PermissionDeniedError` is raised and no permission
        row is written.
        """
        if not await self._meta.check_permission(granter_id, namespace_id, path_prefix, "admin"):
            raise PermissionDeniedError(
                f"principal {granter_id!r} lacks admin on {path_prefix!r} in namespace {namespace_id!r}"
            )
        perm = Permission(
            id=str(ULID()),
            principal_id=target_principal_id,
            namespace_id=namespace_id,
            path_prefix=path_prefix,
            operations=operations,
            created_at=datetime.now(timezone.utc),
        )
        await self._meta.set_permission(perm)
        await audit_permission_change(
            self._meta,
            namespace_id=namespace_id,
            principal_id=granter_id,
            target_principal_id=target_principal_id,
            path_prefix=path_prefix,
            operations=operations,
            audit_log_enabled=self._config.audit_log_enabled,
        )

    async def bootstrap_admin(self, principal_id: str, namespace_id: str) -> None:
        """One-time grant of `admin` on `/` to the first admin in an empty namespace.

        Rejected with `PermissionDeniedError` if any principal already holds
        admin in this namespace — the bootstrap door closes as soon as the
        first admin exists. Subsequent grants must go through the gated
        `grant()` path.
        """
        if await self._meta.has_any_admin(namespace_id):
            raise PermissionDeniedError(
                f"bootstrap_admin rejected: namespace {namespace_id!r} already has at least one admin"
            )
        perm = Permission(
            id=str(ULID()),
            principal_id=principal_id,
            namespace_id=namespace_id,
            path_prefix="/",
            operations={"admin"},
            created_at=datetime.now(timezone.utc),
        )
        await self._meta.set_permission(perm)
        event = AuditEvent(
            event_id=str(ULID()),
            timestamp=datetime.now(timezone.utc),
            namespace_id=namespace_id,
            principal_id=principal_id,
            operation="bootstrap_admin",
            detail={"path_prefix": "/", "operations": ["admin"]},
        )
        await audit(self._meta, event, audit_log_enabled=self._config.audit_log_enabled)

    async def create_namespace(self, display_name: str, created_by: str) -> Namespace:
        """Create and register a namespace, returning the Namespace record."""
        ns = Namespace(
            id=str(ULID()),
            display_name=display_name,
            created_at=datetime.now(timezone.utc),
            created_by=created_by,
        )
        # set_name() is the uniqueness gate: a duplicate display_name raises ConflictError
        # and writes nothing, so claiming the name BEFORE persisting the namespace prevents
        # an orphan entity even on a best-effort no-op transaction() (standalone Mongo). On a
        # transactional store the whole block still rolls back atomically.
        async with self._meta.transaction():
            await self._meta.set_name("namespace", ns.id, display_name)
            await self._meta.put_namespace(ns)
        return ns

    async def create_principal(self, display_name: str, principal_type: str = "agent") -> Principal:
        """Create and register a principal (UUID4 id), returning the Principal record."""
        import uuid

        p = Principal(
            id=str(uuid.uuid4()),
            display_name=display_name,
            principal_type=principal_type,
            created_at=datetime.now(timezone.utc),
        )
        # set_name() is the uniqueness gate: a duplicate display_name raises ConflictError
        # and writes nothing, so claiming the name BEFORE persisting the principal prevents
        # an orphan entity even on a best-effort no-op transaction() (standalone Mongo). On a
        # transactional store the whole block still rolls back atomically.
        async with self._meta.transaction():
            await self._meta.set_name("principal", p.id, display_name)
            await self._meta.put_principal(p)
        return p

    async def resolve_name(self, entity_type: str, display_name: str) -> str | None:
        """Return the entity ID for a display name, or None."""
        return await self._meta.resolve_name(entity_type, display_name)
