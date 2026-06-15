"""VFS orchestrator — the main entry point for file system operations."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import importlib
import importlib.util
import inspect
import logging
import posixpath
import re
import time

import blake3
from ulid import ULID

from vfs.config import VFSConfig
from vfs.errors import (
    AnchorConflictError,
    ConflictError,
    IndexUnavailableError,
    NotFoundError,
    OperationBudgetExceededError,
    PermissionDeniedError,
    ReadBudgetExceededError,
    ReindexRequiredError,
    SearchTypeUnsupportedError,
    VersionCollisionError,
)
from vfs.models import (
    AuditEvent,
    FileMeta,
    Namespace,
    Permission,
    Principal,
    SearchArtifact,
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
from vfs.protocols.search import FindPredicates, SearchLimits, SearchMetaEntry, SearchRequest
from vfs.search.default import DefaultSearchProvider
from vfs.search.reader import ContentReader
from vfs.stores.cached_blob import CachedBlobStore
from vfs.stores.local_blob import LocalFSBlobStore
from vfs.stores.sqlite_metadata import SQLiteMetadataStore

_log = logging.getLogger(__name__)

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


_MAX_WRITE_RETRIES: int = 5
"""Maximum number of times VFS write/copy/move retries on a version-number collision."""


def _straggler_regex_results(path: str, text: str, query: str) -> list:
    """Return per-line SearchResult entries for ``text`` matching ``query`` (REGEX).

    Mirrors ``DefaultSearchProvider._regex_search`` semantics: one result per
    matching line, ``line_number`` 1-based, ``match_context`` is the stripped line.
    Returns an empty list when ``query`` is not a valid regex.
    """
    from vfs.models import SearchResult

    try:
        compiled = re.compile(query)
    except re.error:
        return []
    results = []
    for line_num, line in enumerate(text.splitlines(), start=1):
        if compiled.search(line):
            results.append(SearchResult(path=path, line_number=line_num, match_context=line.strip()))
    return results


def _require_canonical(path: str) -> None:
    """Reject paths that are not absolute or not in canonical form.

    A canonical path starts with ``/`` and, after stripping at most one trailing ``/``
    (the root ``/`` is exempt from stripping), equals ``posixpath.normpath(path)``.
    This accepts the root ``/``, bare absolute paths like ``/foo``, and directory-style
    paths like ``/foo/``.  It rejects paths with ``..``, ``.`` segments, or repeated
    slashes anywhere in the path.

    Raises ValueError immediately, before any permission check or storage access.
    """
    if not path.startswith("/"):
        raise ValueError(f"path must be absolute, got {path!r}")
    check = path[:-1] if (path.endswith("/") and path != "/") else path
    if check != posixpath.normpath(check):
        raise ValueError(f"path must be canonical (no '..' or '.' segments, no repeated slashes); got {path!r}")


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
        """Close storage connections.

        The inner blob store may expose either a synchronous or an asynchronous
        ``close()``; both are handled. Adapters without any ``close()`` (e.g.
        :class:`LocalFSBlobStore`) are tolerated via the ``getattr(..., None)`` guard.
        The inner store is released BEFORE the disk-cache wrapper so a remote-adapter's
        connection pool is torn down deterministically while the cache wrapper is still
        intact.
        """
        await self._meta.close()
        inner = self._blob._inner if isinstance(self._blob, CachedBlobStore) else self._blob
        closer = getattr(inner, "close", None)
        if closer is not None:
            result = closer()
            if inspect.isawaitable(result):
                await result
        if isinstance(self._blob, CachedBlobStore):
            self._blob.close()

    # --- Permission helper ---

    async def _check_perm(self, principal_id: str, namespace_id: str, path: str, operation: str) -> None:
        if not await self._meta.check_permission(principal_id, namespace_id, path, operation):
            raise PermissionDeniedError(f"Principal {principal_id!r} lacks {operation!r} on {namespace_id}:{path}")

    # --- stat / list ---

    async def stat(self, namespace_id: str, path: str, *, principal_id: str) -> FileMeta:
        """Return file metadata. Raises PermissionDeniedError or NotFoundError."""
        _require_canonical(path)
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
        _require_canonical(path_prefix)
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
        _require_canonical(path)
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

            # Non-native provider indexing (DefaultSearchProvider returns None; kept for
            # future custom providers that implement index() without NativeTextSearch).
            existing = await self._meta.get_file(namespace_id, path)
            file_meta = existing or FileMeta(
                namespace_id=namespace_id,
                path=path,
                current_version_id="",
                current_version_number=0,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            _artifact = await self._search.index(path, content, file_meta)
            # External boundary: SearchProvider.index() must return SearchArtifact | None.
            # A raw dict return is the pre-phase2-search protocol; raise a clear TypeError
            # rather than letting the AttributeError surface deep in the call chain.
            if _artifact is not None and not isinstance(_artifact, SearchArtifact):
                raise TypeError(
                    f"SearchProvider.index() must return SearchArtifact | None; "
                    f"{type(self._search).__name__!r} returned {type(_artifact).__name__!r}. "
                    "Update the provider to use SearchArtifact (phase2-search protocol change)."
                )
            base_search_meta: dict = {}
            if _artifact is not None:
                base_search_meta = {_artifact.provider_key: _artifact.to_dict()}

            # Native text search: decode content once (content is immutable across retries).
            nts = self._meta.native_text_search()
            nts_text: str | None = None
            nts_unsupported_meta: dict | None = None
            if nts is not None:
                try:
                    nts_text = content.decode("utf-8")
                except UnicodeDecodeError:
                    # Content-level failure: record an unsupported artifact; write succeeds.
                    _unsupported = SearchArtifact(
                        status="unsupported",
                        schema_version=1,
                        provider_key=nts.provider_key,
                        provider_version="1",
                        params_hash=nts.params_hash,
                        content_hash=content_hash,
                        created_at=datetime.now(timezone.utc),
                        storage="inline",
                        error_code="decode_error",
                        error_message="content is not valid UTF-8",
                    )
                    nts_unsupported_meta = {nts.provider_key: _unsupported.to_dict()}

            # Retry on VersionCollisionError (concurrent no-CAS write took the same
            # version_number). Re-read current state each attempt so the next try uses N+2
            # when N+1 was taken by a racing writer. CAS writes (expected_version set) are
            # not retried — the caller owns the conflict semantics.
            version: VersionMeta | None = None
            for attempt in range(_MAX_WRITE_RETRIES):
                if attempt > 0:
                    existing = await self._meta.get_file(namespace_id, path)
                version_number = 1 if existing is None else existing.current_version_number + 1
                now = datetime.now(timezone.utc)
                version_id = str(ULID())

                try:
                    if nts is not None:
                        # index_text and put_version share one transaction so a rollback
                        # of either leaves no orphan text artifact or version row.
                        async with self._meta.transaction():
                            if nts_text is not None:
                                nts_artifact = await nts.index_text(
                                    version_id, content_hash, nts.params_hash, nts_text
                                )
                                version_search_meta = {
                                    **base_search_meta,
                                    nts.provider_key: nts_artifact.to_dict(),
                                }
                            else:
                                version_search_meta = {**base_search_meta, **(nts_unsupported_meta or {})}
                            version = VersionMeta(
                                id=version_id,
                                file_path=path,
                                namespace_id=namespace_id,
                                version_number=version_number,
                                content_hash=content_hash,
                                size=len(content),
                                created_at=now,
                                created_by=principal_id,
                                search_meta=version_search_meta,
                            )
                            await self._meta.put_version(version, expected_version=expected_version)
                    else:
                        version = VersionMeta(
                            id=version_id,
                            file_path=path,
                            namespace_id=namespace_id,
                            version_number=version_number,
                            content_hash=content_hash,
                            size=len(content),
                            created_at=now,
                            created_by=principal_id,
                            search_meta=base_search_meta,
                        )
                        await self._meta.put_version(version, expected_version=expected_version)
                    break
                except VersionCollisionError:
                    if expected_version is not None or attempt == _MAX_WRITE_RETRIES - 1:
                        raise

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
        _require_canonical(path)
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
        _require_canonical(path)
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
        _require_canonical(src)
        _require_canonical(dst)
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

            new_version: VersionMeta | None = None
            for attempt in range(_MAX_WRITE_RETRIES):
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
                try:
                    await self._meta.put_version(new_version, expected_version=expected_version)
                    break
                except VersionCollisionError:
                    if expected_version is not None or attempt == _MAX_WRITE_RETRIES - 1:
                        raise

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
        _require_canonical(src)
        _require_canonical(dst)
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

            new_version: VersionMeta | None = None
            _new: VersionMeta | None = None
            # Tracks the dst version_number we attempted on the last iteration so we can
            # detect whether dst committed on a best-effort (Mongo) store before the
            # tombstone write failed — avoiding a double-insert of the destination on retry.
            _last_dst_attempt: int | None = None

            for attempt in range(_MAX_WRITE_RETRIES):
                # Re-read src_file inside the loop so the tombstone version_number is always
                # fresh.  Without this, a collision on the tombstone write causes every
                # subsequent attempt to re-use the same stale version_number and re-collide.
                src_file = await self._meta.get_file(namespace_id, src)
                dst_file = await self._meta.get_file(namespace_id, dst)

                # On best-effort stores (standalone Mongo) dst may have committed on a prior
                # attempt while the tombstone write collided.  Detect this by checking whether
                # dst advanced to the version_number we last tried; if so, skip re-inserting it.
                dst_committed = (
                    _last_dst_attempt is not None
                    and dst_file is not None
                    and dst_file.current_version_number >= _last_dst_attempt
                )
                if not dst_committed:
                    dst_version_number = (dst_file.current_version_number + 1) if dst_file else 1
                    _last_dst_attempt = dst_version_number
                    now = datetime.now(timezone.utc)
                    _new = VersionMeta(
                        id=str(ULID()),
                        file_path=dst,
                        namespace_id=namespace_id,
                        version_number=dst_version_number,
                        content_hash=src_version.content_hash,
                        size=src_version.size,
                        created_at=now,
                        created_by=principal_id,
                    )
                else:
                    now = datetime.now(timezone.utc)

                _tombstone = VersionMeta(
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
                try:
                    async with self._meta.transaction():
                        # Create at destination FIRST. On a best-effort no-op transaction()
                        # (standalone Mongo) each put_version commits on its own, so writing the
                        # destination before the source tombstone makes a mid-move failure
                        # non-destructive (a duplicate, never a loss) per the MoveFile spec; on a
                        # transactional store the whole block still rolls back atomically.
                        if not dst_committed:
                            await self._meta.put_version(_new)
                        await self._meta.put_version(_tombstone)
                    new_version = _new
                    break
                except VersionCollisionError:
                    if attempt == _MAX_WRITE_RETRIES - 1:
                        raise

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
        _require_canonical(path)
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
        _require_canonical(path)
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
                # Copy search_meta from target: content-addressed external artifacts
                # still resolve because the text record is keyed by content_hash, not
                # version_id — so the copied artifact_ref remains valid after rollback.
                search_meta=target.search_meta,
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
        find_predicates: FindPredicates | None = None,
    ) -> list[SearchResult]:
        """Search files in scope, silently pruning results the principal cannot read.

        Dispatch rules
        --------------
        - ``GLOB`` / ``FIND``: always served by :class:`~vfs.search.default.DefaultSearchProvider`
          from metadata (no blob reads).
        - ``REGEX`` / ``FULLTEXT``: routed to :meth:`~vfs.protocols.metadata.MetadataStore.native_text_search`
          when the active store exposes it.  When the capability is absent:

          - ``REGEX``: falls back to the ``DefaultSearchProvider`` brute-force path, which
            uses the guarded reader and enforces ``max_content_reads`` (large-scope regex
            fails loud via :class:`~vfs.errors.ReadBudgetExceededError`).
          - ``FULLTEXT``: raises :class:`~vfs.errors.SearchTypeUnsupportedError` — no
            brute-force equivalent exists for unranked full-text search.

        Dispatch rules
        --------------
        - ``GLOB`` / ``FIND``: always served by :class:`~vfs.search.default.DefaultSearchProvider`
          from metadata (no blob reads).
        - ``REGEX`` / ``FULLTEXT``: routed to :meth:`~vfs.protocols.metadata.MetadataStore.native_text_search`
          when the active store exposes it (SQLite FTS5, PostgreSQL pg_trgm + tsvector).
          When absent:

          - ``REGEX``: falls back to the ``DefaultSearchProvider`` brute-force path via
            the guarded reader (budget-bounded; may raise ``ReadBudgetExceededError``).
          - ``FULLTEXT``: raises :class:`~vfs.errors.SearchTypeUnsupportedError`.

        Tension with delta spec MongoRegexDeferred scenario
        ---------------------------------------------------
        The delta spec ``MongoRegexDeferred`` scenario states that both regex *and* fulltext
        are rejected as unsupported for MongoDB.  The dispatch rule implemented here is more
        permissive for regex: absent a native capability, regex falls back to
        ``DefaultSearchProvider`` brute-force for any backend.  The dispatch cannot
        currently distinguish Mongo from SQLite at the VFS level — both return ``None``
        from ``native_text_search()``.  The MongoRegexDeferred test encodes the rule as
        implemented: fulltext → unsupported; regex → brute-force.
        """
        _require_canonical(scope)
        t0 = time.monotonic()
        with vfs_span(
            "search",
            {"vfs.namespace": namespace_id, "vfs.path": scope, "vfs.principal_id": principal_id},
            otel_enabled=self._config.otel_enabled,
        ):
            # Early rejection of truly unsupported search types (e.g. SEMANTIC) before
            # any storage work, preserving the pre-existing ValueError contract.
            _supported = {SearchType.GLOB, SearchType.FIND, SearchType.REGEX, SearchType.FULLTEXT}
            if search_type not in _supported:
                raise ValueError(
                    f"No search provider supports {search_type.value!r}. "
                    f"Available: {sorted(t.value for t in _supported)}"
                )

            # List all files in scope and permission-prune (invisible)
            all_files = await self._meta.list_dir(namespace_id, scope, recursive=True)
            pruned_files = [
                f for f in all_files if await self._meta.check_permission(principal_id, namespace_id, f.path, "read")
            ]
            record_search_candidates(
                len(pruned_files),
                {"vfs.namespace": namespace_id, "vfs.search_type": search_type.value},
                otel_enabled=self._config.otel_enabled,
            )

            # Build permission-pruned SearchMetaEntry list.
            # Each entry carries the current version's content_hash (for the guarded
            # reader) and its search_meta manifest (for artifact usability checks).
            entries: list[SearchMetaEntry] = []
            for f in pruned_files:
                ver = await self._meta.get_version(namespace_id, f.path)
                if ver is None or ver.is_tombstone:
                    continue
                entries.append(
                    SearchMetaEntry(
                        version_id=ver.id,
                        path=f.path,
                        content_hash=ver.content_hash,
                        size=ver.size,
                        updated_at=f.updated_at,
                        is_deleted=f.is_deleted,
                        search_meta=ver.search_meta,
                    )
                )

            # Guarded reader — resolves paths to enumerated content_hash, enforces budget
            limits = SearchLimits()
            reader = ContentReader(entries=entries, blob=self._blob, max_reads=limits.max_content_reads)

            request = SearchRequest(
                query=query,
                scope=scope,
                search_type=search_type,
                search_metas=entries,
                read_content=reader,
                limits=limits,
                find_predicates=find_predicates,
            )

            # Dispatch: route by search type and available capability
            if search_type in (SearchType.GLOB, SearchType.FIND):
                # Metadata-only; always via DefaultSearchProvider, no blob reads
                response = await self._search.search(request)
            else:
                # REGEX or FULLTEXT: prefer the native capability; fall back otherwise
                nts = self._meta.native_text_search()
                if nts is not None:
                    response = await self._native_search(
                        nts=nts,
                        entries=entries,
                        request=request,
                        reader=reader,
                        namespace_id=namespace_id,
                        search_type=search_type,
                        query=query,
                        limits=limits,
                    )
                elif search_type == SearchType.FULLTEXT:
                    raise SearchTypeUnsupportedError(
                        f"fulltext search requires the NativeTextSearch capability; "
                        f"the active metadata store ({type(self._meta).__name__}) does not "
                        f"expose it. Use a SQLite or PostgreSQL store for fulltext support."
                    )
                else:
                    # REGEX without native capability: brute-force via DefaultSearchProvider.
                    # The guarded reader enforces max_content_reads so large-scope regex
                    # fails loud via ReadBudgetExceededError rather than issuing unbounded reads.
                    response = await self._search.search(request)

            record_op(
                "search",
                (time.monotonic() - t0) * 1000,
                {"vfs.namespace": namespace_id},
                otel_enabled=self._config.otel_enabled,
            )
            return response.results

    # --- execute ---

    async def execute(
        self,
        code: str,
        namespace_id: str,
        principal_id: str,
        provider_name: str,
        *,
        timeout: float | None = None,
        resource_limits=None,
        cwd: str = "/",
    ):
        """Execute ``code`` in a sandboxed provider, returning an ``ExecutionResult``.

        Two-tier error contract
        -----------------------
        **Tier 1** — raises before any session, FsOperations, or provider is constructed:

        - ``ValueError`` for malformed arguments (non-canonical ``cwd``) or unknown
          provider name.
        - ``ImportError`` (actionable "install ai-vfs[extra]") when the provider's
          optional dependency is absent.
        - ``PermissionDeniedError`` when the principal lacks ``execute`` permission on
          ``cwd`` — consistent with every other VFS operation.

        **Tier 2** — all exceptions arising *after* dispatch begins are translated to
        ``ExecutionResult(success=False, ...)``; no raw traceback, host path, or
        adapter-internal detail appears in ``error_message``.
        """
        from vfs.execution.anchors import AnchorMap
        from vfs.execution.fs_ops import fs_operations_for
        from vfs.execution.registry import resolve_execution_provider
        from vfs.protocols.execution import ExecutionResult, ResourceLimits
        from vfs.session import Session

        # --- Tier 1: validate args and check permission (raises, never returns ExecutionResult) ---
        _require_canonical(cwd)

        # Resolve effective resource limits: config defaults → caller overrides.
        effective_limits: ResourceLimits
        if resource_limits is not None:
            effective_limits = resource_limits
        else:
            effective_limits = ResourceLimits(
                timeout_seconds=self._config.default_timeout_seconds,
                max_operations=self._config.default_max_operations,
            )

        effective_timeout = timeout if timeout is not None else effective_limits.timeout_seconds

        # Permission check on cwd (raises PermissionDeniedError — Tier 1).
        # Must precede provider construction so that no provider is instantiated
        # when the principal lacks execute permission (per ExecuteRequiresPermission spec).
        await self._check_perm(principal_id, namespace_id, cwd, "execute")

        # Resolve provider after permission check (raises ValueError / ImportError — Tier 1).
        provider = resolve_execution_provider(provider_name, self._config)

        # --- Construct session, anchor map, and FsOperations ---
        session = Session(self, namespace_id, principal_id)
        # session.cd enforces read permission on cwd; permission denied here is a
        # caller-side error, so let it propagate (Tier 1 boundary).
        await session.cd(cwd)
        anchor_map = AnchorMap()
        fs_ops = fs_operations_for(session, effective_limits, anchor_map)

        # --- Tier 2: wrap provider dispatch; translate all execution-time exceptions ---
        try:
            result = await asyncio.wait_for(
                provider.execute(code, fs_ops, effective_limits),
                timeout=effective_timeout,
            )
        except asyncio.TimeoutError:
            return ExecutionResult(success=False, error_type="timeout", error_message="Execution timed out")
        except PermissionDeniedError:
            return ExecutionResult(
                success=False,
                error_type="permission_denied",
                error_message="Access denied to path",
            )
        except NotFoundError:
            return ExecutionResult(success=False, error_type="not_found", error_message="File not found")
        except ConflictError:
            return ExecutionResult(
                success=False,
                error_type="conflict",
                error_message="Version conflict; re-read and retry",
            )
        except VersionCollisionError:
            return ExecutionResult(
                success=False,
                error_type="conflict",
                error_message="Concurrent write; retry",
            )
        except OperationBudgetExceededError:
            return ExecutionResult(
                success=False,
                error_type="budget_exceeded",
                error_message="Operation limit reached",
            )
        except AnchorConflictError:
            return ExecutionResult(
                success=False,
                error_type="anchor_conflict",
                error_message="Anchors stale; re-read file",
            )
        except ReadBudgetExceededError:
            return ExecutionResult(
                success=False,
                error_type="search_unavailable",
                error_message="Search read budget exhausted; reindex",
            )
        except ReindexRequiredError:
            return ExecutionResult(
                success=False,
                error_type="search_unavailable",
                error_message="Index cold; run vfs.reindex()",
            )
        except IndexUnavailableError:
            return ExecutionResult(
                success=False,
                error_type="search_unavailable",
                error_message="Search index unavailable",
            )
        except Exception:  # noqa: BLE001
            _log.exception("Unexpected error during vfs.execute for principal %s", principal_id)
            return ExecutionResult(success=False, error_type="internal_error", error_message="Execution error")
        else:
            return result

    # --- Native search (straggler path) ---

    async def _native_search(
        self,
        *,
        nts,
        entries,
        request,
        reader,
        namespace_id: str,
        search_type,
        query: str,
        limits,
    ):
        """Serve a REGEX/FULLTEXT search via the NativeTextSearch capability.

        Classifies each visible entry as *fresh* (usable artifact), *confirmed non-match*
        (identity-matched ``unsupported`` artifact — binary content), or *straggler*
        (missing, failed, or stale artifact).  Fresh entries are served by the capability
        without any blob read.  Confirmed non-matches are skipped entirely and do not
        count against the straggler budget.  Stragglers are verified individually via the
        guarded reader up to ``limits.max_content_reads``.

        Fresh entries whose external text record has been deleted out-of-band (detected
        via ``nts.has_text_artifacts``) are reclassified as stragglers so they are
        verified individually rather than silently missed.

        Raises
        ------
        ReindexRequiredError
            When the straggler count exceeds ``limits.max_content_reads`` — serving
            the search would require unbounded blob reads.  Run ``vfs.reindex()``
            to rebuild the index.
        IndexUnavailableError
            When the index store raises during the capability search call.  Run
            ``vfs.reindex()`` after the store issue is resolved.

        Note on fulltext straggler scores
        ----------------------------------
        Fresh fulltext results are ranked by the native BM25/ts_rank score.  Straggler
        fulltext results use a naive token-AND match and are appended with score=1.0
        after the ranked fresh results.  The merged list is therefore not globally
        sorted by relevance when stragglers are present.
        """
        # Classify entries: fresh (usable), confirmed non-match (binary), or straggler
        fresh_entries = []
        straggler_entries = []
        for entry in entries:
            artifact_dict = entry.search_meta.get(nts.provider_key)
            if artifact_dict is None:
                straggler_entries.append(entry)
                continue
            try:
                artifact = SearchArtifact.from_dict(artifact_dict)
                # B2: An identity-matched 'unsupported' artifact (content_hash and
                # params_hash current) is a confirmed non-match for text predicates —
                # binary content cannot satisfy any regex or fulltext query.  Skip it
                # without counting against the straggler budget.
                if (
                    artifact.status == "unsupported"
                    and artifact.content_hash == entry.content_hash
                    and artifact.params_hash == nts.params_hash
                ):
                    continue  # confirmed non-match — excluded from budget
                if artifact.is_usable(
                    current_content_hash=entry.content_hash,
                    active_params_hash=nts.params_hash,
                ):
                    fresh_entries.append(entry)
                else:
                    straggler_entries.append(entry)
            except Exception:  # noqa: BLE001 — malformed artifact treated as straggler
                straggler_entries.append(entry)

        # S2: Verify that external text records exist for fresh entries (detects out-of-band
        # deletes from search_text_artifacts).  Entries whose record is missing are
        # reclassified as stragglers so they are verified via the guarded reader rather than
        # silently missed.  All current NTS implementations use 'external' storage, so this
        # check covers all fresh entries.
        if fresh_entries:
            unique_fresh_hashes = list({e.content_hash for e in fresh_entries})
            found_hashes = await nts.has_text_artifacts(unique_fresh_hashes, nts.params_hash)
            still_fresh = []
            for entry in fresh_entries:
                if entry.content_hash in found_hashes:
                    still_fresh.append(entry)
                else:
                    straggler_entries.append(entry)
            fresh_entries = still_fresh

        # Fail loud when straggler count would require unbounded reads
        if len(straggler_entries) > limits.max_content_reads:
            raise ReindexRequiredError(
                f"{len(straggler_entries)} file(s) in the search scope lack a fresh "
                f"index artifact (budget: {limits.max_content_reads}). "
                f"Run vfs.reindex({namespace_id!r}) to rebuild the index."
            )

        # Search fresh entries via native capability (zero blob reads)
        fresh_request = SearchRequest(
            query=request.query,
            scope=request.scope,
            search_type=request.search_type,
            search_metas=fresh_entries,
            read_content=request.read_content,
            limits=request.limits,
            find_predicates=request.find_predicates,
        )
        fresh_version_ids = [e.version_id for e in fresh_entries]
        try:
            response = await nts.search_text(fresh_request, fresh_version_ids)
        except Exception as exc:
            raise IndexUnavailableError(
                f"The native search index is unavailable. "
                f"Run vfs.reindex({namespace_id!r}) after resolving the store issue. "
                f"Cause: {exc}"
            ) from exc

        # Verify stragglers via guarded reader; backfill index after verification
        for entry in straggler_entries:
            content = await reader.read(entry.path)
            # Strict UTF-8: binary content cannot be regex/fulltext searched
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                # B2: binary straggler — best-effort backfill an 'unsupported' artifact so
                # future searches skip it without counting it against the budget.
                try:
                    _unsupported = SearchArtifact(
                        status="unsupported",
                        schema_version=1,
                        provider_key=nts.provider_key,
                        provider_version="1",
                        params_hash=nts.params_hash,
                        content_hash=entry.content_hash,
                        created_at=datetime.now(timezone.utc),
                        storage="inline",
                        error_code="decode_error",
                        error_message="content is not valid UTF-8",
                    )
                    await self._meta.update_search_artifact(entry.version_id, nts.provider_key, _unsupported)
                except Exception as exc:  # noqa: BLE001 — best-effort
                    _log.debug("Failed to record unsupported artifact for %s: %s", entry.path, exc)
                continue

            # Apply predicate against decoded text.
            # REGEX: per-line results (line_number, match_context) via helper.
            # FULLTEXT: whole-text predicate; single path-only result (no per-line
            # context for fulltext stragglers — documented behaviour).
            if search_type == SearchType.REGEX:
                response.results.extend(_straggler_regex_results(entry.path, text, query))
            else:  # FULLTEXT: all query tokens must appear in the text (best-effort)
                tokens = query.lower().split()
                text_lower = text.lower()
                if bool(tokens) and all(tok in text_lower for tok in tokens):
                    response.results.append(SearchResult(path=entry.path))

            # Lazy backfill: persist the verified text so future searches are fresh.
            # Best-effort — a failure here must not fail the search.
            try:
                async with self._meta.transaction():
                    backfill_artifact = await nts.index_text(
                        entry.version_id, entry.content_hash, nts.params_hash, text
                    )
                    await self._meta.update_search_artifact(entry.version_id, nts.provider_key, backfill_artifact)
            except Exception as exc:  # noqa: BLE001 — best-effort backfill
                _log.debug("Lazy backfill failed for %s: %s", entry.path, exc)

        return response

    # --- GC / reindex ---

    async def run_gc(self, namespace_id: str | None = None):
        """Run garbage collection and return a GCResult."""
        from vfs.gc import GarbageCollector

        gc = GarbageCollector(self._meta, self._blob, self._config)
        return await gc.run(namespace_id)

    async def reindex(self, namespace_id: str, provider_name: str = "default", scope: str = "/") -> int:
        """Backfill search metadata for files in scope; returns the count of versions updated.

        When the metadata store exposes the ``NativeTextSearch`` capability,
        ``index_text`` is called for each file inside a transaction so the text
        artifact and the updated ``search_meta`` are committed atomically.
        Binary (non-UTF-8) files receive an ``unsupported`` artifact.

        Falls back to ``DefaultSearchProvider.index()`` when the native
        capability is absent (which always returns ``None`` for the default
        provider, so ``count`` only increments on non-default providers).
        """
        _require_canonical(scope)
        nts = self._meta.native_text_search()
        files = await self._meta.list_dir(namespace_id, scope, recursive=True)
        count = 0
        for f in files:
            ver = await self._meta.get_version(namespace_id, f.path)
            if ver is None or ver.is_tombstone:
                continue
            content = await self._blob.get(ver.content_hash)
            if nts is not None:
                try:
                    text = content.decode("utf-8")
                    async with self._meta.transaction():
                        artifact = await nts.index_text(ver.id, ver.content_hash, nts.params_hash, text)
                        await self._meta.update_search_artifact(ver.id, nts.provider_key, artifact)
                except UnicodeDecodeError:
                    # Content-level: persist an unsupported artifact so future searches
                    # know this file cannot be text-indexed.
                    unsupported = SearchArtifact(
                        status="unsupported",
                        schema_version=1,
                        provider_key=nts.provider_key,
                        provider_version="1",
                        params_hash=nts.params_hash,
                        content_hash=ver.content_hash,
                        created_at=datetime.now(timezone.utc),
                        storage="inline",
                        error_code="decode_error",
                        error_message="content is not valid UTF-8",
                    )
                    await self._meta.update_search_artifact(ver.id, nts.provider_key, unsupported)
                count += 1
            else:
                artifact = await self._search.index(f.path, content, f)
                if artifact is not None:
                    await self._meta.update_search_artifact(ver.id, artifact.provider_key, artifact)
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
        _require_canonical(path_prefix)
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
