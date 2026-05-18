# Phase 1: Core Library — Tasks

> **Implementation note:** Use `superpowers:test-driven-development` skill when implementing.
> Each task follows Red → Green → Commit.
> Run tests with: `uv run pytest tests/` (all) or `uv run pytest -n auto tests/unit/` (parallel unit tests).
>
> **Spec refs** resolve to this change's delta specs at `.specs/changes/phase1-core/specs/<capability>/spec.md`.
> Baseline is empty until this bootstrap change is synced.

---

## Group 1: Foundation

### Task 1: Domain models and exceptions

**Files:**

- Create: `src/vfs/models.py`
- Create: `src/vfs/errors.py`
- Create: `tests/unit/test_models.py`

**Spec refs:** `storage/PydanticSettingsConfig`, `file-operations/ULIDIdentifiers`,
`versioning/ImmutableVersionHistory`, `access-control/OperationGranularity`

- [x] Write `tests/unit/test_models.py` — assert construction, field types, and
  ULID round-trip for `FileMeta`, `VersionMeta`, `Permission`, `AuditEvent`,
  `RetentionPolicy`, `SearchResult`; assert `ConflictError`, `PermissionDeniedError`,
  `NotFoundError` are all subclasses of a base `VFSError`

- [x] Run tests — confirm they fail (`ModuleNotFoundError` or `ImportError`)

- [x] Implement `src/vfs/errors.py`:

  ```python
  class VFSError(Exception): ...


  class ConflictError(VFSError): ...  # CAS mismatch


  class PermissionDeniedError(VFSError): ...


  class NotFoundError(VFSError): ...
  ```

- [x] Implement `src/vfs/models.py` — Pydantic `BaseModel` for all entities:

  - `SearchType`: `Enum` with `GLOB`, `FIND`, `REGEX`, `FULLTEXT`, `SEMANTIC`
  - `RetentionTier`: `max_age: timedelta`, `keep_every: timedelta | None`
  - `RetentionPolicy`: `max_recent_versions=50`, `tiers` (default Time Machine tiers),
    `keep_first_version=True`, `keep_current_version=True`
  - `FileMeta`: `namespace_id: str` (ULID str), `path: str`,
    `current_version_id: str`, `current_version_number: int`,
    `created_at: datetime`, `updated_at: datetime`, `is_deleted: bool = False`
  - `VersionMeta`: `id: str`, `file_path: str`, `namespace_id: str`,
    `version_number: int`, `content_hash: str`, `size: int`,
    `created_at: datetime`, `created_by: str`, `is_tombstone: bool = False`,
    `search_meta: dict = {}`, `parent_version_id: str | None = None`
  - `Permission`: `id: str`, `principal_id: str`, `namespace_id: str`,
    `path_prefix: str`, `operations: set[str]`, `created_at: datetime`
  - `AuditEvent`: `event_id: str`, `timestamp: datetime`, `namespace_id: str`,
    `principal_id: str`, `operation: str`, `path: str | None = None`,
    `version_id: str | None = None`, `detail: dict = {}`,
    `trace_id: str | None = None`
  - `SearchResult`: `path: str`, `line_number: int | None`, `match_context: str | None`,
    `score: float = 1.0`
  - `Namespace`: `id: str`, `display_name: str`, `created_at: datetime`,
    `created_by: str`, `retention_policy: RetentionPolicy | None = None`
  - `Principal`: `id: str`, `display_name: str`, `principal_type: str`, `created_at: datetime`
  - ID generation rule (design D12): use `str(uuid.uuid4())` for person-related entities or any entity where leaking creation time (concrete or relative) could be exploitable; use `str(ULID())` for everything else.
    For current entities: `Principal.id` → UUID4, all other entity IDs → ULID.
    Foreign keys that reference principals (`VersionMeta.created_by`, `Permission.principal_id`, `AuditEvent.principal_id`, `Namespace.created_by`) store the referenced principal's UUID4 — not a ULID — even though the referencing record itself has a ULID.
    The column type stays `str` either way.

- [x] Run tests — confirm they pass

- [x] Commit: `feat(vfs): add domain models and exceptions`

---

### Task 2: Configuration

**Files:**

- Create: `src/vfs/config.py`
- Create: `tests/unit/test_config.py`

**Spec refs:** `storage/PydanticSettingsConfig`, `storage/URIBasedStoreResolution`

- [x] Write `tests/unit/test_config.py` — assert defaults match spec
  (SQLite URI, local FS URI, `otel_enabled=True`, `audit_log_enabled=True`,
  `search_providers=["default"]`, `blob_cache_enabled=None`);
  assert `AIFS_METADATA_STORE_URI` env var overrides `metadata_store_uri`

- [x] Run tests — confirm they fail

- [x] Implement `src/vfs/config.py`:

  ```python
  from pydantic_settings import BaseSettings, SettingsConfigDict


  class VFSConfig(BaseSettings):
      model_config = SettingsConfigDict(env_prefix="AIFS_")

      metadata_store_uri: str = "sqlite:///./aifs.db"
      blob_store_uri: str = "file:///./aifs_blobs/"
      blob_cache_enabled: bool | None = None  # None = auto (True for remote, False for local)
      blob_cache_max_size_mb: int = 1024
      blob_cache_dir: str | None = None  # None = auto (system temp dir)
      retention_max_recent: int = 50
      retention_tiers: list[dict] | None = None
      otel_enabled: bool = True
      audit_log_enabled: bool = True
      search_providers: list[str] = ["default"]
      execution_providers: list[str] = []
      default_timeout_seconds: float = 30.0
      default_max_operations: int = 1000
  ```

- [x] Run tests — confirm they pass

- [x] Commit: `feat(vfs): add VFSConfig with pydantic-settings`

---

### Task 3: Protocol definitions

**Files:**

- Create: `src/vfs/protocols/__init__.py`
- Create: `src/vfs/protocols/metadata.py`
- Create: `src/vfs/protocols/blob.py`
- Create: `src/vfs/protocols/search.py`

**Spec refs:** `storage/MetadataStoreProtocol`, `storage/BlobStoreProtocol`, `search/PluggableSearchProviders`

- [x] Implement `src/vfs/protocols/blob.py`:

  ```python
  from typing import AsyncIterator, Protocol, runtime_checkable


  @runtime_checkable
  class BlobStore(Protocol):
      async def put(self, content_hash: str, data: bytes) -> None: ...
      async def get(self, content_hash: str) -> bytes: ...
      async def delete(self, content_hash: str) -> None: ...
      async def exists(self, content_hash: str) -> bool: ...
      async def put_stream(self, content_hash: str, stream: AsyncIterator[bytes]) -> None: ...
      async def get_stream(self, content_hash: str) -> AsyncIterator[bytes]: ...
  ```

- [x] Implement `src/vfs/protocols/metadata.py` — `MetadataStore(Protocol)` with all
  methods from design doc section 3.1 (put_file, get_file, delete_file, list_dir,
  put_version, get_version, list_versions, check_permission, set_permission,
  append_audit_event, update_search_meta, set_name, resolve_name,
  list_reclaimable_versions, delete_versions) plus a `transaction()` async context
  manager for atomic multi-step operations (D14: used by move)

- [x] Implement `src/vfs/protocols/search.py`:

  ```python
  from typing import Protocol, runtime_checkable
  from vfs.models import FileMeta, SearchResult, SearchType


  @runtime_checkable
  class SearchProvider(Protocol):
      async def index(self, path: str, content: bytes, metadata: FileMeta) -> dict: ...
      async def search(
          self,
          query: str,
          scope: str,
          search_type: SearchType,
          candidates: list[FileMeta] | None = None,
      ) -> list[SearchResult]: ...
      def capabilities(self) -> set[SearchType]: ...
  ```

- [x] No tests needed for protocols (structural typing — tested via adapter conformance
  in later tasks); add `isinstance(impl, BlobStore)` checks in adapter tests

- [x] Commit: `feat(vfs): add MetadataStore, BlobStore, SearchProvider protocols`

---

## Group 2: Storage Adapters

### Task 4: LocalFSBlobStore

**Files:**

- Create: `src/vfs/stores/__init__.py`
- Create: `src/vfs/stores/local_blob.py`
- Create: `tests/unit/test_local_blob.py`

**Spec refs:** `storage/BlobStoreProtocol`, `storage/BlobIdempotentPut`,
`storage/BlobPrefixDirectoryStructure`, `storage/StreamingProvisions`

- [x] Write `tests/unit/test_local_blob.py` using `pytest.mark.asyncio` and `tmp_path`:

  - `test_put_and_get`: hash, put, get → bytes match
  - `test_put_idempotent`: put same hash twice → no error, content unchanged
  - `test_exists`: True after put, False before
  - `test_delete`: get after delete raises `NotFoundError`
  - `test_prefix_directory_structure`: put hash `"abcdef1234..."` → file at
    `{base}/ab/cd/abcdef1234...`
  - `test_put_stream_raises`: `put_stream` raises `NotImplementedError`
  - `test_conforms_to_protocol`: `assert isinstance(store, BlobStore)`

- [x] Run tests — confirm they fail

- [x] Implement `src/vfs/stores/local_blob.py`:

  ```python
  import aiofiles
  from pathlib import Path
  from vfs.errors import NotFoundError
  from vfs.protocols.blob import BlobStore  # for isinstance check


  class LocalFSBlobStore:
      def __init__(self, base_path: str | Path) -> None:
          self._base = Path(base_path)

      def _path(self, content_hash: str) -> Path:
          return self._base / content_hash[0:2] / content_hash[2:4] / content_hash

      async def put(self, content_hash: str, data: bytes) -> None:
          p = self._path(content_hash)
          if p.exists():
              return
          p.parent.mkdir(parents=True, exist_ok=True)
          async with aiofiles.open(p, "wb") as f:
              await f.write(data)

      async def get(self, content_hash: str) -> bytes:
          p = self._path(content_hash)
          if not p.exists():
              raise NotFoundError(f"blob {content_hash!r} not found")
          async with aiofiles.open(p, "rb") as f:
              return await f.read()

      async def delete(self, content_hash: str) -> None:
          self._path(content_hash).unlink(missing_ok=True)

      async def exists(self, content_hash: str) -> bool:
          return self._path(content_hash).exists()

      async def put_stream(self, content_hash: str, stream) -> None:
          raise NotImplementedError

      async def get_stream(self, content_hash: str):
          raise NotImplementedError
  ```

- [x] Run tests — confirm they pass

- [x] Commit: `feat(vfs): add LocalFSBlobStore with prefix directory structure`

---

### Task 5: CachedBlobStore

**Files:**

- Create: `src/vfs/stores/cached_blob.py`
- Create: `tests/unit/test_cached_blob.py`

**Spec refs:** `storage/BlobCaching`

- [x] Write `tests/unit/test_cached_blob.py`:

  - `test_cache_miss_fetches_from_inner`: get on cold cache → fetches from inner store
  - `test_cache_hit_skips_inner`: get after put → inner store's `get` not called
    (mock inner store with call tracking)
  - `test_write_through`: put → both inner store and cache contain the blob
  - `test_conforms_to_protocol`: `assert isinstance(cached, BlobStore)`
  - `test_diskcache_wraps_in_thread`: ensure `get` and `set` do not block event loop
    (verify with `asyncio.to_thread` pattern — check no sync calls on the loop)

- [x] Run tests — confirm they fail

- [x] Implement `src/vfs/stores/cached_blob.py`:

  ```python
  import asyncio
  from diskcache import Cache
  from vfs.protocols.blob import BlobStore


  class CachedBlobStore:
      def __init__(
          self,
          inner: BlobStore,
          cache_dir: str,
          max_size_mb: int = 1024,
      ) -> None:
          self._inner = inner
          self._cache = Cache(
              cache_dir,
              size_limit=max_size_mb * 1024 * 1024,
              eviction_policy="least-recently-used",
          )

      async def put(self, content_hash: str, data: bytes) -> None:
          await self._inner.put(content_hash, data)
          await asyncio.to_thread(self._cache.set, content_hash, data)

      async def get(self, content_hash: str) -> bytes:
          hit = await asyncio.to_thread(self._cache.get, content_hash)
          if hit is not None:
              return hit
          data = await self._inner.get(content_hash)
          await asyncio.to_thread(self._cache.set, content_hash, data)
          return data

      async def delete(self, content_hash: str) -> None:
          await self._inner.delete(content_hash)
          await asyncio.to_thread(self._cache.delete, content_hash)

      async def exists(self, content_hash: str) -> bool:
          hit = await asyncio.to_thread(self._cache.get, content_hash)
          if hit is not None:
              return True
          return await self._inner.exists(content_hash)

      async def put_stream(self, content_hash: str, stream) -> None:
          raise NotImplementedError

      async def get_stream(self, content_hash: str):
          raise NotImplementedError

      def close(self) -> None:
          self._cache.close()
  ```

- [x] Run tests — confirm they pass

- [x] Commit: `feat(vfs): add CachedBlobStore with diskcache LRU`

---

### Task 6: SQLiteMetadataStore — schema and initialization

**Files:**

- Create: `src/vfs/stores/sqlite_metadata.py`
- Create: `tests/unit/test_sqlite_metadata.py` (initial)

**Spec refs:** `storage/MetadataStoreProtocol`

- [x] Write `tests/unit/test_sqlite_metadata.py` — `test_initialize_creates_tables`:
  open `:memory:` SQLite, call `initialize()`, assert all 7 tables exist
  via `SELECT name FROM sqlite_master WHERE type='table'`
- [x] Run tests — confirm they fail
- [x] Implement `SQLiteMetadataStore.__init__` and `initialize()` in
  `src/vfs/stores/sqlite_metadata.py` — create all tables and indexes from D2
  (design doc); enable WAL mode: `PRAGMA journal_mode=WAL`
- [x] Add `conftest.py` fixture `sqlite_store` (async, yields initialized
  `SQLiteMetadataStore(":memory:")`) for reuse in subsequent tasks
- [x] Run tests — confirm they pass
- [x] Commit: `feat(vfs): add SQLiteMetadataStore schema and initialization`

---

### Task 7: SQLiteMetadataStore — file and version operations with CAS

**Files:**

- Modify: `src/vfs/stores/sqlite_metadata.py`
- Modify: `tests/unit/test_sqlite_metadata.py`

**Spec refs:** `storage/MetadataCASSemantics`, `file-operations/WriteCreatesVersion`,
`file-operations/OptimisticConcurrency`, `versioning/ImmutableVersionHistory`

- [x] Add tests:
  - `test_put_and_get_file`: put then get → same `FileMeta`
  - `test_get_file_missing`: returns `None`
  - `test_list_dir_non_recursive`: create 3 files, list prefix `/src/` → 2 matching
  - `test_list_dir_recursive`: create nested files, `recursive=True` → all
  - `test_put_version_first`: put version with `expected_version=None` → inserts file row
  - `test_put_version_cas_ok`: put version 2 with `expected_version=1` → succeeds
  - `test_put_version_cas_conflict`: put version 2 with `expected_version=99` → `ConflictError`
  - `test_get_version_latest`: `version_number=None` → most recent
  - `test_get_version_by_number`: exact version number lookup
  - `test_list_versions`: returns ordered list, `before` cursor works
- [x] Run tests — confirm they fail
- [x] Implement `put_file`, `get_file`, `delete_file`, `list_dir`, `put_version`,
  `get_version`, `list_versions` in `SQLiteMetadataStore`:
  - `put_version` logic:
    1. `INSERT INTO versions (...)` unconditionally
    2. If `expected_version is None`: `INSERT INTO files (...)` with `ON CONFLICT DO UPDATE`
    3. If `expected_version is not None`: `UPDATE files SET ... WHERE namespace_id=? AND path=? AND current_version_number=?`; if `rowcount == 0` raise `ConflictError`
  - `list_dir` uses `LIKE path_prefix || '%'`; non-recursive additionally filters
    out paths containing `/` after the prefix
- [x] Run tests — confirm they pass
- [x] Commit: `feat(vfs): add SQLiteMetadataStore file/version CRUD with CAS`

---

### Task 8: SQLiteMetadataStore — permissions

**Files:**

- Modify: `src/vfs/stores/sqlite_metadata.py`
- Modify: `tests/unit/test_sqlite_metadata.py`

**Spec refs:** `access-control/DefaultDeny`, `access-control/PathPrefixPermissions`,
`access-control/OperationGranularity`

- [x] Add tests:
  - `test_check_permission_no_rules`: returns `False` for principal with no entries
  - `test_check_permission_matching_prefix`: permission on `/` → returns `True` for `/any/path`
  - `test_check_permission_most_specific`: permission `read` on `/`, `write` on `/workspace/`;
    check `write` on `/workspace/file.txt` → `True`; check `write` on `/other/` → `False`
  - `test_set_and_get_permission`: round-trip via `set_permission` + `check_permission`
  - `test_namespace_isolation`: permission in ns A, check in ns B → `False`
- [x] Run tests — confirm they fail
- [x] Implement `check_permission` and `set_permission`:
  - `check_permission`: fetch all `Permission` rows for `(principal_id, namespace_id)`,
    sort by `len(path_prefix)` descending, return `True` if first matching entry
    contains `operation`, `False` otherwise
  - `set_permission`: `INSERT OR REPLACE INTO permissions (...)`
- [x] Run tests — confirm they pass
- [x] Commit: `feat(vfs): add permission check with most-specific-prefix enforcement`

---

### Task 9: SQLiteMetadataStore — audit, search meta, names, GC

**Files:**

- Modify: `src/vfs/stores/sqlite_metadata.py`
- Modify: `tests/unit/test_sqlite_metadata.py`

**Spec refs:** `observability/AuditLogAppendOnly`, `search/SearchMetadataExtensible`,
`access-control/HumanFriendlyNames`, `versioning/VersionGarbageCollection`

- [x] Add tests:
  - `test_append_audit_event`: event is persisted; second append adds a second row
  - `test_audit_not_updatable`: no `update_audit_event` method on the store (audit-only)
  - `test_update_search_meta`: put version, update `search_meta`, fetch version → meta matches
  - `test_set_and_resolve_name`: `set_name` then `resolve_name` → same ID string
    (ULID for namespaces, UUID4 for principals — `set_name`/`resolve_name` are
    entity-type agnostic; cover at least one of each)
  - `test_resolve_name_missing`: `resolve_name` unknown name → `None`
  - `test_list_reclaimable_versions`: create file with 3 versions, policy `max_recent=1`
    → 2 older versions returned
  - `test_delete_versions`: delete version IDs → not in `list_versions` afterward
- [x] Run tests — confirm they fail
- [x] Implement `append_audit_event`, `update_search_meta`, `set_name`, `resolve_name`,
  `list_reclaimable_versions`, `delete_versions`
  - `list_reclaimable_versions`: apply retention logic — keep the N most recent non-tombstone
    versions per file; return the rest as reclaimable; always keep version 1 and current
  - `delete_versions`: `DELETE FROM versions WHERE id IN (...)`
- [x] Add `test_conforms_to_protocol`: `assert isinstance(store, MetadataStore)`
- [x] Run tests — confirm they pass
- [x] Commit: `feat(vfs): add audit, search meta, names, GC queries to SQLiteMetadataStore`

---

## Group 3: Search

### Task 10: DefaultSearchProvider

**Files:**

- Create: `src/vfs/search/__init__.py`
- Create: `src/vfs/search/default.py`
- Create: `tests/unit/test_default_search.py`

**Spec refs:** `search/GlobSearch`, `search/FindSearch`, `search/RegexContentSearch`,
`search/PluggableSearchProviders`, `search/SearchIndexing`

- [x] Write `tests/unit/test_default_search.py` (all tests pass file content via
  `candidates` list with pre-loaded `content` bytes — provider is a pure function):
  - `test_capabilities`: returns `{SearchType.GLOB, SearchType.FIND, SearchType.REGEX}`
  - `test_glob_non_recursive`: `search("*.py", "/src/", GLOB, candidates)` — only
    direct children match
  - `test_glob_recursive`: `"**/*.py"` with nested candidates → all `.py` files
  - `test_find_by_name`: predicate `name=*.py` → only matching files
  - `test_regex_match`: content contains `"TODO: fix this"` → result with
    `line_number=3`, `match_context` containing `"fix this"`
  - `test_regex_no_match`: empty result list
  - `test_index_returns_empty_dict`: `index()` → `{}`
  - `test_conforms_to_protocol`: `assert isinstance(provider, SearchProvider)`
- [x] Run tests — confirm they fail
- [x] Implement `src/vfs/search/default.py`:
  - `capabilities()` returns `{SearchType.GLOB, SearchType.FIND, SearchType.REGEX}`
  - `index()` returns `{}`
  - `search(query, scope, search_type, candidates)`:
    - `GLOB`: `fnmatch.fnmatch` against paths scoped to prefix; `**` triggers recursive
    - `FIND`: metadata predicates (name pattern, size comparison) against `candidates`
    - `REGEX`: `re.search(query, line)` on each line of decoded content;
      build `SearchResult(path, line_number, match_context)` per match
- [x] Run tests — confirm they pass
- [x] Commit: `feat(vfs): add DefaultSearchProvider (glob, find, regex)`

---

## Group 4: Observability

### Task 11: OTel tracing helpers

**Files:**

- Create: `src/vfs/observability/__init__.py`
- Create: `src/vfs/observability/tracing.py`
- Create: `tests/unit/test_observability.py`

**Spec refs:** `observability/OTelSpansOnAllOperations`, `observability/OTelMetrics`,
`observability/OTelContextPropagation`, `observability/NoOpWhenDisabled`

- [x] Write `tests/unit/test_observability.py`:

  - `test_span_created_when_enabled`: `otel_enabled=True` → `tracer.start_as_current_span`
    is called (use `unittest.mock.patch` on `trace.get_tracer`)
  - `test_no_span_when_disabled`: `otel_enabled=False` → instrumentation function body
    not entered (no OTel calls)
  - `test_metrics_counter_incremented`: `op_counter.add` called with operation label
  - `test_metrics_duration_histogram_recorded`: `op_histogram.record` called with duration
  - `test_metrics_blob_size_histogram_recorded`: `blob_histogram.record` called with byte count
  - `test_no_error_when_otel_not_configured`: run with no SDK configured → no exceptions

- [x] Run tests — confirm they fail

- [x] Implement `src/vfs/observability/tracing.py`:

  ```python
  from opentelemetry import trace, metrics

  _tracer = trace.get_tracer("vfs")
  _meter = metrics.get_meter("vfs")

  op_counter = _meter.create_counter("vfs.operation.count", unit="1")
  op_histogram = _meter.create_histogram("vfs.operation.duration", unit="ms")
  blob_histogram = _meter.create_histogram("vfs.blob.size", unit="By")
  ```

  - `vfs_span(operation: str, attrs: dict, otel_enabled: bool)` — context manager:
    - If `otel_enabled=False`: `yield None` (no-op, no OTel imports exercised)
    - If `otel_enabled=True`: `with _tracer.start_as_current_span(f"vfs.{operation}", attributes=attrs) as span: yield span`
  - `record_op(operation: str, duration_ms: float, attrs: dict, otel_enabled: bool)`:
    records `op_counter.add(1, ...)` and `op_histogram.record(duration_ms, ...)`
    only when `otel_enabled=True`

- [x] Run tests — confirm they pass

- [x] Commit: `feat(vfs): add OTel tracing helpers with no-op when disabled`

---

### Task 12: Audit log helper

**Files:**

- Create: `src/vfs/observability/audit.py`
- Modify: `tests/unit/test_observability.py`

**Spec refs:** `observability/AuditLogStateChanges`, `observability/AuditLogAppendOnly`,
`observability/AuditOTelCorrelation`

- [x] Add tests:

  - `test_audit_write_creates_event`: `audit_write(meta_store, ...)` → `append_audit_event`
    called with `operation="write"`
  - `test_audit_read_not_called`: no audit helper for read operations
  - `test_trace_id_in_audit`: active OTel span → `trace_id` field populated on event
  - `test_no_trace_id_without_context`: no span → `trace_id=None`
  - `test_audit_disabled`: `audit_log_enabled=False` → `append_audit_event` not called

- [x] Run tests — confirm they fail

- [x] Implement `src/vfs/observability/audit.py`:

  ```python
  from opentelemetry import trace as otel_trace


  def _current_trace_id() -> str | None:
      span = otel_trace.get_current_span()
      ctx = span.get_span_context()
      if ctx and ctx.trace_id != 0:
          return format(ctx.trace_id, "032x")
      return None


  async def audit(meta_store, event: AuditEvent, *, audit_log_enabled: bool) -> None:
      if not audit_log_enabled:
          return
      event = event.model_copy(update={"trace_id": _current_trace_id()})
      await meta_store.append_audit_event(event)
  ```

  - Helper constructors: `audit_write(...)`, `audit_delete(...)`, `audit_rollback(...)`,
    `audit_permission_change(...)`, `audit_gc_run(...)` — each builds the appropriate
    `AuditEvent` and calls `audit()`

- [x] Run tests — confirm they pass

- [x] Commit: `feat(vfs): add audit log helper with OTel trace ID correlation`

---

## Group 5: VFS Orchestrator

### Task 13: VFS class — structure, URI resolution, lifecycle

**Files:**

- Create: `src/vfs/vfs.py`
- Create: `tests/unit/test_vfs_uri_resolution.py`
- Create: `tests/conftest.py`

**Spec refs:** `storage/URIBasedStoreResolution`, `storage/ProcessIdentification`

- [x] Write `tests/unit/test_vfs_uri_resolution.py`:
  - `test_sqlite_uri_resolves`: `VFS(config)` with `metadata_store_uri="sqlite:///..."` →
    `store` is instance of `SQLiteMetadataStore`
  - `test_file_uri_resolves`: `blob_store_uri="file:///..."` → `LocalFSBlobStore`
  - `test_cache_disabled_for_local_fs`: `blob_cache_enabled=None` + local URI → no cache wrap
  - `test_cache_enabled_for_unknown_scheme`: `blob_cache_enabled=True` → `CachedBlobStore` wrap
  - `test_unknown_uri_raises`: `metadata_store_uri="badscheme://..."` → `ValueError`
- [x] Add `tests/conftest.py` with `vfs_instance` fixture (tmp_path, SQLite, LocalFS)
- [x] Run tests — confirm they fail
- [x] Implement `src/vfs/vfs.py`:
  - `VFS.__init__(config: VFSConfig | None = None)`: resolve metadata store and blob store
    from URI; auto-enable cache for non-local blob stores when `blob_cache_enabled is None`
  - `VFS.initialize()`: call `meta_store.initialize()`, call `setproctitle("ai-vfs: service")`
    only if `set_proc_title=True` kwarg
  - `VFS.close()`: close connections
- [x] Run tests — confirm they pass
- [ x] Commit: `feat(vfs): add VFS class with URI resolution and lifecycle`

---

### Task 14: VFS.stat and VFS.list

**Files:**

- Modify: `src/vfs/vfs.py`
- Create: `tests/integration/test_vfs_file_operations.py`

**Spec refs:** `file-operations/LazyContentResolution`, `access-control/InvisiblePruning`,
`access-control/DefaultDeny`, `file-operations/NamespaceIsolation`

- [x] Write integration tests using `vfs_instance` fixture:
  - `test_stat_returns_metadata`: write a file, stat it → `FileMeta` returned, no blob access
  - `test_stat_not_found`: stat nonexistent path → `NotFoundError`
  - `test_stat_permission_denied`: principal has no permissions → `PermissionDeniedError`
  - `test_list_non_recursive`: 3 files in `/src/`, 1 nested; list `/src/` non-recursive → 3
  - `test_list_recursive`: list with `recursive=True` → all 4
  - `test_list_invisible_pruning`: 2 files, principal can read only 1 → only 1 returned
  - `test_list_namespace_isolation`: namespace A + namespace B; list in A → only A files
- [x] Run tests — confirm they fail
- [x] Implement `VFS.stat` and `VFS.list`:
  - Both call `meta_store.check_permission(principal_id, namespace_id, path, "read")`
  - `stat`: returns `FileMeta` or raises `NotFoundError`
  - `list`: calls `meta_store.list_dir(...)` then filters results by permission;
    paths the principal cannot read are silently excluded (invisible pruning)
- [x] Run tests — confirm they pass
- [x] Commit: `feat(vfs): add VFS.stat and VFS.list with invisible pruning`

---

### Task 15: VFS.write

**Files:**

- Modify: `src/vfs/vfs.py`
- Modify: `tests/integration/test_vfs_file_operations.py`

**Spec refs:** `file-operations/ContentAddressedStorage`, `file-operations/WriteCreatesVersion`,
`file-operations/OptimisticConcurrency`, `observability/OTelSpansOnAllOperations`,
`observability/AuditLogStateChanges`, `search/SearchIndexing`

- [x] Add integration tests:

  - `test_write_returns_version_meta`: write → returns `VersionMeta`
  - `test_write_creates_new_version`: write twice → version numbers 1 and 2
  - `test_write_content_addressed`: two files with same content → same `content_hash`
  - `test_write_cas_conflict`: write with wrong `expected_version` → `ConflictError`
  - `test_write_permission_denied`: principal lacks write → `PermissionDeniedError`
  - `test_write_creates_audit_event`: `audit_log_enabled=True` → audit event persisted
  - `test_write_updates_search_meta`: search provider returns non-empty dict →
    stored in `version.search_meta`

- [x] Run tests — confirm they fail

- [x] Implement `VFS.write`:

  ```text
  1. check_permission(principal_id, namespace_id, path, "write")
  2. content_hash = blake3.hash(content).hex()  # blake3.Hash.hex(), not .hexdigest()
  3. blob_store.put(content_hash, content)  [idempotent]
  4. version = VersionMeta(id=str(ULID()), version_number=current+1, content_hash=..., ...)
  5. search_meta = await search_provider.index(path, content, file_meta)
  6. version.search_meta = search_meta
  7. meta_store.put_version(version, expected_version=expected_version)
  8. await audit_write(meta_store, ..., audit_log_enabled=config.audit_log_enabled)
  9. [otel span wraps steps 2–8]
  10. return version
  ```

- [x] Run tests — confirm they pass

- [x] Commit: `feat(vfs): add VFS.write with content addressing, versioning, and audit`

---

### Task 16: VFS.read

**Files:**

- Modify: `src/vfs/vfs.py`
- Modify: `tests/integration/test_vfs_file_operations.py`

**Spec refs:** `file-operations/ReadReturnsContent`, `file-operations/LazyContentResolution`,
`observability/AuditLogStateChanges` (reads NOT audited)

- [x] Add integration tests:
  - `test_read_returns_content`: write then read → same bytes
  - `test_read_specific_version`: write twice; read version 1 → first content
  - `test_read_deleted_file`: read after delete → `NotFoundError`
  - `test_read_permission_denied`: read without permission → `PermissionDeniedError`
  - `test_read_not_audited`: read does not create audit event
- [x] Run tests — confirm they fail
- [x] Implement `VFS.read(namespace_id, path, principal_id, version_number=None)`:
  1. `check_permission(..., "read")`
  2. `meta = meta_store.get_version(namespace_id, path, version_number)`
  3. If `meta.is_tombstone` or `meta is None`: raise `NotFoundError`
  4. `return await blob_store.get(meta.content_hash)`
- [x] Run tests — confirm they pass
- [x] Commit: `feat(vfs): add VFS.read with permission check and version selection`

---

### Task 17: VFS.delete

**Files:**

- Modify: `src/vfs/vfs.py`
- Modify: `tests/integration/test_vfs_file_operations.py`

**Spec refs:** `file-operations/DeleteCreatesTombstone`, `observability/AuditLogStateChanges`

- [x] Add integration tests:
  - `test_delete_creates_tombstone`: delete → `stat` returns `is_deleted=True`
  - `test_delete_old_versions_still_accessible`: read version 1 after delete → content returned
  - `test_delete_permission_denied`: principal lacks delete → `PermissionDeniedError`
  - `test_delete_creates_audit_event`: audit event with `operation="delete"` persisted
- [x] Run tests — confirm they fail
- [x] Implement `VFS.delete`:
  1. `check_permission(..., "delete")`
  2. Create tombstone `VersionMeta(is_tombstone=True, content_hash="", size=0, ...)`
  3. `meta_store.put_version(tombstone)`
  4. `audit_delete(...)`
- [x] Run tests — confirm they pass
- [x] Commit: `feat(vfs): add VFS.delete with tombstone versioning and audit`

---

### Task 17b: VFS.copy and VFS.move

**Files:**

- Modify: `src/vfs/vfs.py`
- Modify: `src/vfs/protocols/metadata.py` (add `transaction()` to SQLiteMetadataStore)
- Modify: `src/vfs/stores/sqlite_metadata.py`
- Modify: `tests/integration/test_vfs_file_operations.py`

**Spec refs:** `file-operations/CopyFile`, `file-operations/MoveFile`,
`storage/MetadataTransactions`, `observability/AuditLogStateChanges`

- [x] Add integration tests for copy:

  - `test_copy_to_new_path`: copy /src/a.py → /dst/a.py; dst exists at v1 with same content_hash; src unchanged
  - `test_copy_to_existing_path`: dst already exists → new version written at dst
  - `test_copy_nonexistent_source`: source missing → `NotFoundError`
  - `test_copy_no_blob_duplication`: after copy, blob store has only one blob for the shared hash
  - `test_copy_permission_denied`: principal lacks read on src or write on dst → `PermissionDeniedError`
  - `test_copy_creates_audit_event`: audit event with `operation="copy"` persisted

- [x] Add integration tests for move:

  - `test_move_to_new_path`: move /src/a.py → /dst/a.py; dst at v1 with same content_hash; src is tombstoned
  - `test_move_to_existing_path`: dst already exists → overwritten; src tombstoned
  - `test_move_nonexistent_source`: source missing → `NotFoundError`
  - `test_move_atomicity`: if dst creation fails, src is NOT tombstoned (transaction rollback)
  - `test_move_permission_denied`: principal lacks delete on src or write on dst → `PermissionDeniedError`
  - `test_move_creates_audit_event`: audit events for both tombstone and create

- [x] Run tests — confirm they fail

- [x] Implement `SQLiteMetadataStore.transaction()` async context manager:

  ```python
  @asynccontextmanager
  async def transaction(self):
      async with self._conn.execute("BEGIN"):
          try:
              yield
              await self._conn.execute("COMMIT")
          except Exception:
              await self._conn.execute("ROLLBACK")
              raise
  ```

- [x] Implement `VFS.copy` (D13):

  1. `check_permission(principal_id, namespace_id, src, "read")`
  2. `check_permission(principal_id, namespace_id, dst, "write")`
  3. `src_version = meta_store.get_version(namespace_id, src)`
  4. `meta_store.put_version(VersionMeta(content_hash=src_version.content_hash, size=src_version.size, ...), expected_version=...)` at dst
  5. `audit_copy(...)`

- [x] Implement `VFS.move` (D14):

  1. `check_permission(principal_id, namespace_id, src, "read")`
  2. `check_permission(principal_id, namespace_id, src, "delete")`
  3. `check_permission(principal_id, namespace_id, dst, "write")`
  4. Within `meta_store.transaction()`:
     - Create tombstone on src
     - Create new file/version at dst with src's content_hash
  5. `audit_move(...)`

- [x] Run tests — confirm they pass

- [x] Commit: `feat(vfs): add VFS.copy and VFS.move with atomic move transaction`

---

### Task 18: VFS.versions and VFS.rollback

**Files:**

- Modify: `src/vfs/vfs.py`
- Create: `tests/integration/test_vfs_versioning.py`

**Spec refs:** `versioning/ImmutableVersionHistory`, `versioning/RollbackCreatesNewVersion`,
`versioning/VersionHistoryQuery`, `observability/AuditLogStateChanges`

- [x] Write `tests/integration/test_vfs_versioning.py`:
  - `test_versions_returns_history`: write 3 times → 3 version entries, ordered newest-first
  - `test_versions_limit_and_before`: pagination cursor works
  - `test_rollback_creates_new_version`: write v1, v2; rollback to v1 → v3 with v1's content_hash
  - `test_rollback_is_new_version_not_mutation`: v1 and v3 are separate rows; v2 still exists
  - `test_rollback_read_returns_v1_content`: read after rollback → original v1 bytes
  - `test_rollback_creates_audit_event`: `operation="rollback"` in audit log
  - `test_rollback_permission_denied`: write-only permission → rollback allowed
    (rollback is a write); delete-only → rollback denied
- [x] Run tests — confirm they fail
- [x] Implement `VFS.versions` (calls `meta_store.list_versions`) and `VFS.rollback`:
  - `rollback`: get target version meta → create new `VersionMeta` with
    `content_hash=target.content_hash`, `size=target.size`,
    `parent_version_id=target.id` → `meta_store.put_version(new_version)` →
    audit; no blob copy needed (content-addressed dedup)
- [x] Run tests — confirm they pass
- [x] Commit: `feat(vfs): add VFS.versions and VFS.rollback`

---

### Task 19: VFS.search

**Files:**

- Modify: `src/vfs/vfs.py`
- Create: `tests/integration/test_vfs_search.py`

**Spec refs:** `search/GlobSearch`, `search/FindSearch`, `search/RegexContentSearch`,
`search/PluggableSearchProviders`, `access-control/InvisiblePruning`

- [x] Write `tests/integration/test_vfs_search.py`:
  - `test_glob_search`: 3 files; `search("*.py", scope="/src/", GLOB)` → only `.py` paths
  - `test_find_search`: `search("*.txt", scope="/", FIND)` → name-matched files
  - `test_regex_grep`: file containing `"# TODO"` on line 5;
    `search("TODO", scope="/", REGEX)` → result with `line_number=5`
  - `test_search_scoped_to_permissions`: files in `/public/` and `/secret/`;
    principal read-only on `/public/` → only `/public/` results returned
  - `test_search_provider_dispatch`: glob search dispatched to provider declaring `GLOB`
    capability; if multiple providers match, most capable used
- [x] Run tests — confirm they fail
- [x] Implement `VFS.search`:
  1. List all files in scope via `meta_store.list_dir(namespace_id, scope, recursive=True)`
  2. Filter to paths principal can read (invisible pruning)
  3. Find first provider that declares `search_type` in `capabilities()`
  4. For `REGEX`: fetch blob content for each candidate, pass to provider
  5. For `GLOB` / `FIND`: pass file metadata only (no blob reads)
  6. Return `list[SearchResult]`
- [x] Run tests — confirm they pass
- [x] Commit: `feat(vfs): add VFS.search with provider dispatch and permission pruning`

---

## Group 6: GC and Public API

### Task 20: GarbageCollector

**Files:**

- Create: `src/vfs/gc.py`
- Create: `tests/unit/test_gc.py`

**Spec refs:** `versioning/VersionGarbageCollection`, `versioning/RetentionPolicy`,
`storage/BlobEnumeration`, `observability/AuditLogStateChanges` (GC audited)

- [x] Write `tests/unit/test_gc.py`:

  - `test_version_gc_respects_max_recent`: file with 5 versions, `max_recent=2` →
    3 older versions reclaimed
  - `test_version_gc_keeps_first_version`: `keep_first_version=True` → v1 never reclaimed
  - `test_version_gc_keeps_current`: current version never reclaimed
  - `test_blob_gc_removes_orphaned_blobs`: create version, delete version metadata,
    run blob GC → blob deleted from store
  - `test_blob_gc_keeps_referenced_blobs`: blob referenced by remaining version → not deleted
  - `test_gc_creates_audit_event`: `operation="gc_run"` with counts in `detail`
  - `test_gc_safe_to_skip`: no errors if GC never runs; no correctness dependency

- [x] Run tests — confirm they fail

- [x] Implement `src/vfs/gc.py`:

  ```python
  import setproctitle


  class GarbageCollector:
      def __init__(self, meta_store, blob_store, config: VFSConfig) -> None: ...

      async def run(self, namespace_id: str | None = None) -> GCResult:
          setproctitle.setproctitle("ai-vfs: gc")
          versions_reclaimed = await self._version_gc(namespace_id)
          blobs_reclaimed = await self._blob_gc()
          await audit_gc_run(self._meta_store, ...)
          return GCResult(versions_reclaimed=versions_reclaimed, blobs_reclaimed=blobs_reclaimed)

      async def _version_gc(self, namespace_id) -> int:
          reclaimable = await self._meta_store.list_reclaimable_versions(self._config._retention_policy(), namespace_id)
          ids = [v.id for v in reclaimable]
          await self._meta_store.delete_versions(ids)
          return len(ids)

      async def _blob_gc(self) -> int:
          # Enumerate blob store, check each hash for references
          ...
  ```

  Add `GCResult` dataclass to `models.py`.

- [x] Run tests — confirm they pass

- [x] Commit: `feat(vfs): add GarbageCollector with version and blob GC`

---

### Task 21: VFS.run_gc and VFS.reindex

**Files:**

- Modify: `src/vfs/vfs.py`
- Modify: `tests/integration/test_vfs_file_operations.py`

**Spec refs:** `versioning/VersionGarbageCollection`, `versioning/SearchMetaReindex`

- [x] Add integration tests:
  - `test_run_gc_reclaims_excess_versions`: write 3 versions, `max_recent=1` → run_gc
    → only 1 version in history
  - `test_reindex_backfills_search_meta`: write file, register a mock provider
    that returns `{"test_key": "value"}` from `index()`, call reindex →
    `search_meta.test_key` populated
- [x] Run tests — confirm they fail
- [x] Implement `VFS.run_gc(namespace_id=None)` (delegates to `GarbageCollector.run`)
  and `VFS.reindex(namespace_id, provider_name, scope="/")`:
  - `reindex`: list all files in scope, for each fetch content, call
    `provider.index(path, content, meta)`, update `search_meta` via
    `meta_store.update_search_meta(...)`
- [x] Run tests — confirm they pass
- [x] Commit: `feat(vfs): add VFS.run_gc and VFS.reindex`

---

### Task 22: Public API and integration smoke test

**Files:**

- Create: `src/vfs/__init__.py`
- Create: `tests/integration/test_vfs_access_control.py`

**Spec refs:** `access-control/PermissionGranting`, `access-control/NamespaceBoundary`,
`access-control/HumanFriendlyNames`, `access-control/OperationGranularity`,
`storage/ProcessIdentification`

- [x] Write `tests/integration/test_vfs_access_control.py`:

  - `test_grant_and_use_permission`: admin grants read+write to principal B;
    B can write and read
  - `test_cross_namespace_denied`: principal in namespace A cannot access namespace B
  - `test_admin_grants_subtree`: admin on `/workspace/` grants write on `/workspace/docs/`
    to principal B; B can write there; B cannot write to `/config.yaml`
  - `test_name_resolution_namespace_ulid`: create namespace with display name;
    `vfs.resolve_name("namespace", "my-workspace")` → ULID returned
  - `test_name_resolution_principal_uuid4`: create principal with display name;
    `vfs.resolve_name("principal", "agent-bob")` → UUID4 returned
  - `test_execute_permission_storable`: grant {execute} on /workspace/ to a principal;
    query permissions → execute is in the stored operations set

- [x] Run tests — confirm they fail

- [x] Implement `src/vfs/__init__.py`:

  ```python
  from vfs.vfs import VFS
  from vfs.config import VFSConfig
  from vfs.errors import ConflictError, PermissionDeniedError, NotFoundError, VFSError

  __all__ = ["VFS", "VFSConfig", "ConflictError", "PermissionDeniedError", "NotFoundError", "VFSError"]
  ```

- [x] Add `VFS.grant(principal_id, namespace_id, path_prefix, operations)` helper
  (calls `meta_store.set_permission(Permission(...))`)

- [x] Add `VFS.create_namespace(display_name, created_by)` helper

- [x] Add `VFS.resolve_name(entity_type, display_name)` helper

- [x] Run tests — confirm they pass

- [x] Commit: `feat(vfs): add public API, namespace helpers, permission grant`

---

## Final: Coverage check

- [x] Run full suite: `uv run pytest tests/`
- [x] Run coverage: `uv run pytest --cov=vfs --cov-report=term-missing tests/`
- [x] Verify all spec requirements from the 6 Phase 1 capabilities have at least one
  passing test covering each scenario in: file-operations, versioning, access-control,
  storage, observability, search
- [x] Commit: `chore(vfs): phase1 complete — all tests passing`

---

## Group 7: Verify-driven revisions (post-implementation)

These tasks resolve findings from `sdd-verify` (see `.verify/test-output.log` and the verify report).
They follow spec edits applied to phase1-core (HumanFriendlyNames, FindSearch, PluggableSearchProviders, RetentionPolicy) and new design decisions D15 (`bootstrap_admin`) and D16 (span `principal_id`).

### Task 23: Wire `grant()` admin gate, add `bootstrap_admin`, audit permission changes

**Files:**

- Modify: `src/vfs/vfs.py` (grant signature, bootstrap_admin, audit hookup)
- Modify: `tests/integration/test_vfs_access_control.py`
- Modify: `tests/integration/test_vfs_file_operations.py`, `test_vfs_versioning.py`,
  `test_vfs_search.py` (existing `vfs.grant(...)` calls gain a `granter_id` arg)

**Spec refs:** `access-control/PermissionGranting`, `observability/AuditLogStateChanges`, design D15

- [x] Update `VFS.grant(...)` signature: insert `granter_id: str` as first positional; rename existing `principal_id` to `target_principal_id`.
  Add admin check: `await self._meta.check_permission(granter_id, namespace_id, path_prefix, "admin")` → raise `PermissionDeniedError` if false
- [x] Add `VFS.bootstrap_admin(principal_id, namespace_id)`: query permissions for any
  existing admin in the namespace; raise `PermissionDeniedError("bootstrap consumed")`
  if any exist; otherwise write `Permission(operations={"admin"}, path_prefix="/", ...)`
  and call `audit_permission_change` with `operation="bootstrap_admin"`
- [x] Add `audit_permission_change` call to `grant()` after the `set_permission` write
- [x] Add tests in `test_vfs_access_control.py`:
  - `test_non_admin_cannot_grant`: principal with read+write (no admin) on `/workspace/`
    calls grant → `PermissionDeniedError`; permissions table unchanged
  - `test_admin_can_grant`: admin on `/` grants read+write on `/workspace/` to another
    principal → that principal can write under `/workspace/`
  - `test_bootstrap_admin_creates_first_admin`: fresh namespace; `bootstrap_admin(p)`
    → `p` has admin on `/`; can subsequently `grant(granter_id=p.id, ...)`
  - `test_bootstrap_admin_rejected_when_admin_exists`: second `bootstrap_admin` call
    on the same namespace → `PermissionDeniedError`
  - `test_grant_creates_audit_event`: `grant` writes an `AuditEvent`
    with `operation="permission_change"` to the audit log
  - `test_bootstrap_admin_creates_audit_event`: audit row with
    `operation="bootstrap_admin"`
- [x] Update every existing `vfs.grant(...)` call in tests to use the new signature
  (insert a bootstrapped admin as `granter_id`)
- [x] Run tests — confirm they pass
- [ ] Commit: `feat(vfs): gate grant() with admin check; add bootstrap_admin; audit permission changes`

---

### Task 24: Add `vfs.principal_id` span attribute at all 10 VFS write-sites

**Files:**

- Modify: `src/vfs/vfs.py` (10 `vfs_span(...)` call sites)
- Modify: `tests/unit/test_observability.py` (per-site attribute coverage)

**Spec refs:** `observability/OTelSpansOnAllOperations`, design D16

- [x] At each `vfs_span(operation, attrs, ...)` call site, add `"vfs.principal_id": principal_id`
  to the attrs dict — sites: stat, list, write, read, delete, copy, move, versions, rollback, search
- [x] Add `test_span_attributes_include_principal_id_*` for each of the 10 operations
  in `test_observability.py` (or as a parametrized test) — mocks the tracer and asserts
  `principal_id` appears in span attributes
- [x] Run tests — confirm they pass
- [ ] Commit: `feat(vfs): add principal_id to OTel span attributes at all VFS write-sites`

---

### Task 25: Add `search_candidate_count` metric

**Files:**

- Modify: `src/vfs/observability/tracing.py` (new histogram + helper)
- Modify: `src/vfs/vfs.py` (record after candidate-build in `search()`)
- Modify: `tests/unit/test_observability.py`

**Spec refs:** `observability/OTelMetrics`

- [x] Add `search_candidate_histogram = _meter.create_histogram("vfs.search.candidates", unit="1")`
  in `tracing.py` and `record_search_candidates(count: int, attrs: dict, *, otel_enabled: bool)` helper
- [x] In `VFS.search`, after `candidates = [...]` is built, call
  `record_search_candidates(len(candidates), {"vfs.namespace": namespace_id}, otel_enabled=...)`
- [x] Add `test_search_candidate_count_recorded` asserting the histogram is recorded
- [ ] Commit: `feat(vfs): add search candidate count metric`

---

### Task 26: OTelContextPropagation parent-link test

**Files:**

- Modify: `tests/unit/test_observability.py`

**Spec refs:** `observability/OTelContextPropagation`

- [x] Add `test_vfs_span_links_to_parent_span`: create a parent span via
  `_tracer.start_as_current_span("parent")`; inside it call `vfs_span(...)`;
  capture spans (use an in-memory `InMemorySpanExporter` from `opentelemetry.sdk.trace.export`);
  assert the vfs span's `parent_span_id == parent.get_span_context().span_id`
- [ ] Commit: `test(vfs): verify OTel context propagation links vfs spans to parent`

---

### Task 27: Close remaining CRITICAL test gaps

**Files:**

- Modify: tests under `tests/unit/` and `tests/integration/`

- [x] `test_versions_permission_denied` in `test_vfs_versioning.py` —
  principal with no permissions calls `versions()` → `PermissionDeniedError`

- [x] `test_copy_cas_success` and `test_copy_cas_conflict` in
  `test_vfs_file_operations.py` — exercise the copy `expected_version` branch

- [x] `test_cached_blob_put_idempotent` in `test_cached_blob.py` —
  `cached_store.put(hash, data)` twice; inner is called once (or check no-op semantic)

- [x] `test_initialize_sets_process_title` and `test_gc_run_sets_process_title`
  using `monkeypatch.setattr("setproctitle.setproctitle", capture_fn)` and asserting
  the captured value matches `"ai-vfs: service"` / `"ai-vfs: gc"`

- [x] `test_reindex_backfills_search_meta` in `test_vfs_versioning.py` or new
  `test_vfs_reindex.py` — register a mock provider that returns `{"k": "v"}`,
  call `vfs.reindex(...)`, assert `search_meta` is populated for files in scope

- [x] `test_admin_permission_storable` in `test_vfs_access_control.py` —
  grant `{admin}` to a principal; query permissions; admin is in the set;
  the principal can then call `grant(...)` (covers `OperationGranularity` admin path)

- [ ] Commit: `test(vfs): close CRITICAL gaps from verify (versions perms, copy CAS, cached blob idempotency, proc title, reindex, admin op)`

---

### Task 28: Close WARNING test gaps

**Files:**

- Modify: tests under `tests/unit/` and `tests/integration/`

- [x] `test_audit_log_survives_gc` — write a few audits, run version+blob GC,
  assert audit rows are unchanged (count + content)

- [x] `test_write_dedup_single_blob` — write identical content under two paths,
  assert `blob_store.list_hashes()` yields one hash (proves DeduplicatedWrite at VFS layer)

- [x] `test_stat_does_not_fetch_blob` and `test_list_does_not_fetch_blob` —
  wrap blob_store with a spy; call stat/list; assert spy never called (LazyContentResolution)

- [x] `test_write_cas_success_at_vfs` — write twice with `expected_version=1` on the
  second call → new version 2 (VFS-level happy path complement to existing conflict test)

- [x] `test_namespace_id_is_ulid_format` + `test_version_id_is_ulid_format` —
  assert `len(ns.id) == 26` and `len(ver.id) == 26` (ULID base32 string)

- [x] `test_cached_blob_list_hashes` — `CachedBlobStore.list_hashes()` delegates to inner

- [x] `test_write_does_not_mutate_prior_version` — write v1; capture all fields; write v2;
  re-fetch v1; assert all immutable fields are byte-identical

- [x] `test_rollback_sets_parent_version_id` — rollback to v1; new version's
  `parent_version_id == v1.id`

- [x] `test_gc_cross_namespace_blob_preservation` — two files in different namespaces
  share a content_hash; GC one namespace's old version; blob still exists

- [ ] Commit: `test(vfs): close WARNING gaps from verify (audit/GC isolation, dedup count, lazy I/O, CAS happy, ULID format, immutability, rollback link, x-ns GC)`

---

### Task 29: Re-run `sdd-verify`

- [x] Re-run `sdd-verify` per the verify skill workflow
- [x] Confirm zero CRITICAL findings remain
- [ ] Commit: `chore(vfs): close phase1 verify findings; ready to sync`
