# Phase 1: Core Library — Design

**Change:** `phase1-core` **Date:** 2026-04-04

## Context

The baseline specs define all required behavior.
The design document (`.specs/ai-vfs-design-doc.md`) resolves all major architectural questions.
This document captures implementation-level decisions not covered in the spec.

## Decisions

### D1: Module import path is `vfs`, not `ai_vfs`

**Rationale:** `pyproject.toml` sets `module-name = "vfs"` under `[tool.uv.build-backend]`.
The design doc's `from ai_vfs import VFS` is aspirational — actual imports are `from vfs import VFS`.

**Alternatives considered:** Renaming the module to `ai_vfs` — rejected to avoid a pyproject.toml change in Phase 1.

---

### D2: SQLite schema uses two tables for files + versions

Files and versions are separate tables.
`files` holds the current pointer; `versions` is the immutable append-only log.

```sql
CREATE TABLE IF NOT EXISTS namespaces (
    id          TEXT PRIMARY KEY,        -- ULID
    display_name TEXT NOT NULL,
    created_at  TEXT NOT NULL,           -- ISO 8601
    created_by  TEXT NOT NULL,           -- principal UUID4
    retention_policy TEXT                -- JSON or NULL
);

CREATE TABLE IF NOT EXISTS principals (
    id          TEXT PRIMARY KEY,        -- UUID4 (no embedded timestamp; prevents ordering inference)
    display_name TEXT NOT NULL,
    principal_type TEXT NOT NULL,        -- "agent" | "user" | "service"
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    namespace_id         TEXT NOT NULL,
    path                 TEXT NOT NULL,
    current_version_id   TEXT NOT NULL,  -- ULID
    current_version_number INTEGER NOT NULL,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    is_deleted           INTEGER NOT NULL DEFAULT 0,  -- 0/1
    PRIMARY KEY (namespace_id, path)
);

CREATE TABLE IF NOT EXISTS versions (
    id               TEXT PRIMARY KEY,   -- ULID
    file_path        TEXT NOT NULL,
    namespace_id     TEXT NOT NULL,
    version_number   INTEGER NOT NULL,
    content_hash     TEXT NOT NULL,      -- BLAKE3 hex
    size             INTEGER NOT NULL,
    created_at       TEXT NOT NULL,
    created_by       TEXT NOT NULL,      -- principal ULID
    is_tombstone     INTEGER NOT NULL DEFAULT 0,
    search_meta      TEXT NOT NULL DEFAULT '{}',  -- JSON
    parent_version_id TEXT,              -- ULID or NULL
    UNIQUE (namespace_id, file_path, version_number)
);

CREATE TABLE IF NOT EXISTS permissions (
    id           TEXT PRIMARY KEY,       -- ULID
    principal_id TEXT NOT NULL,
    namespace_id TEXT NOT NULL,
    path_prefix  TEXT NOT NULL,          -- e.g., "/" or "/workspace/"
    operations   TEXT NOT NULL,          -- JSON array: ["read","write",...]
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id     TEXT PRIMARY KEY,       -- ULID
    timestamp    TEXT NOT NULL,
    namespace_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    operation    TEXT NOT NULL,
    path         TEXT,
    version_id   TEXT,
    detail       TEXT NOT NULL DEFAULT '{}',  -- JSON
    trace_id     TEXT
);

CREATE TABLE IF NOT EXISTS names (
    entity_type  TEXT NOT NULL,
    entity_id    TEXT NOT NULL,          -- ULID for file-system entities; UUID4 for principals
    display_name TEXT NOT NULL,
    PRIMARY KEY (entity_type, entity_id),
    UNIQUE (entity_type, display_name)
);
```

Key indexes:

```sql
CREATE INDEX IF NOT EXISTS idx_versions_ns_path ON versions (namespace_id, file_path, version_number DESC);
CREATE INDEX IF NOT EXISTS idx_permissions_principal ON permissions (principal_id, namespace_id);
CREATE INDEX IF NOT EXISTS idx_audit_ns_time ON audit_events (namespace_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_versions_hash ON versions (content_hash);  -- for blob GC
```

---

### D3: CAS uses UPDATE rowcount, not SELECT + UPDATE

`put_version` performs an atomic `UPDATE files SET ... WHERE namespace_id=? AND path=? AND current_version_number=?`.
If `rowcount == 0`, it raises `ConflictError`.
No SELECT is needed first — the WHERE clause is the check.

For new files (no prior version), `put_version` performs an `INSERT INTO files`.

This is idiomatic SQLite optimistic concurrency — no transactions beyond the
single statement are needed for the check itself.

---

### D4: Permission check is a sorted prefix scan

```python
# Fetch all permissions for (principal_id, namespace_id) — typically O(1–10)
# Sort by path_prefix length descending (most-specific first)
# Take the first entry whose path_prefix is a prefix of the requested path
# Check that operation is in that entry's operations set
```

Permissions are small per-principal sets; a full scan with Python sort is correct
and avoids complex SQL for the most-specific-prefix logic.

---

### D5: OTel integration uses the API unconditionally; `otel_enabled=False` skips instrumentation

The OpenTelemetry Python API returns no-op tracer/meter when no SDK is installed.
When `otel_enabled=True` the VFS calls the OTel API normally.
When `otel_enabled=False` the VFS skips all instrumentation calls entirely (zero overhead, no imports beyond the check).

This provides two distinct behaviors:

- Consumer-configured SDK, `otel_enabled=True` → real spans exported
- No SDK, `otel_enabled=True` → OTel API no-ops automatically
- `otel_enabled=False` → VFS code never calls OTel at all

---

### D6: `CachedBlobStore` uses `diskcache.Cache` with `size_limit`

```python
from diskcache import Cache

cache = Cache(directory=str(cache_dir), size_limit=max_size_mb * 1024 * 1024)
# cache[content_hash] = data        # write-through on put
# data = cache.get(content_hash)    # None on miss
```

The cache wraps any `BlobStore`.
On `get`: check cache first; on miss, fetch from inner store and populate cache.
On `put`: write to inner store, then write to cache.
`diskcache` handles LRU eviction automatically when `size_limit` is reached.

---

### D7: `LocalFSBlobStore` uses `aiofiles` for all I/O

Blob path: `{base_path}/{hash[0:2]}/{hash[2:4]}/{hash}`.

Idempotent put: check `exists()` before writing.
Content-addressed blobs are immutable so a pre-existence check is race-safe (two concurrent writers of the same hash produce identical data).

```python
async def put(self, content_hash: str, data: bytes) -> None:
    blob_path = self._path(content_hash)
    if blob_path.exists():  # idempotent: same data, skip
        return
    blob_path.parent.mkdir(parents=True, exist_ok=True)
    async with aiofiles.open(blob_path, "wb") as f:
        await f.write(data)
```

---

### D8: Search providers receive a `fetch_content` callback for lazy blob access

The `SearchProvider.search()` protocol accepts an optional `fetch_content: ContentFetcher` callback.
The VFS creates a closure over namespace + blob store and passes it to the provider.
Providers call it on demand to retrieve file content; metadata-only strategies (glob, find) ignore it.

This design supports three content-access patterns:

- **Brute-force** (DefaultSearchProvider regex): calls `fetch_content` for every candidate
- **Prefiltered** (future bloom provider): runs bloom index first, calls `fetch_content` only for passing candidates
- **Index-only** (future semantic provider): never calls `fetch_content`; queries its own precomputed index

Glob and find remain metadata-only (no blob reads).
The default provider's regex grep is intentionally brute-force — the bloom provider (Phase 2) optimizes this.

---

### D9: GC is two-phase; blob GC uses reference counting via SQL

Phase 1 — version GC:

- Query `list_reclaimable_versions(policy, namespace_id)` → list of `VersionMeta`
- `delete_versions(version_ids)` removes metadata rows
- Log `AuditEvent(operation="gc_run", detail={"versions_reclaimed": N})`

Phase 2 — blob GC:

- Query all `content_hash` values present in `versions` table
- Compare against blobs in the blob store
- Delete blobs with zero remaining version references

```sql
-- Content hashes with no remaining version references
SELECT DISTINCT content_hash
FROM versions
GROUP BY content_hash
HAVING COUNT(*) = 0  -- never true; use NOT IN instead:

-- Blobs to delete: exist in store but not referenced by any version
-- (The GC iterates blob store and checks each hash against versions table)
SELECT 1 FROM versions WHERE content_hash = ? LIMIT 1
-- If no row: blob is orphaned, safe to delete
```

Blob GC must be conservative: a hash may be referenced by versions in multiple namespaces.
Only delete when `SELECT 1 FROM versions WHERE content_hash = ?` returns no rows.

---

### D10: `VFS` stores resolver is a simple URI-prefix dispatch

```python
# Phase 1: only SQLite + local FS
_METADATA_SCHEMES = {
    "sqlite:///": SQLiteMetadataStore,
}
_BLOB_SCHEMES = {
    "file:///": LocalFSBlobStore,
}
# Phase 2 will register: postgresql://, mongodb://, s3://
```

Resolution is a linear prefix scan at `VFS.__init__` time.
Unknown URIs raise `ValueError` with a clear message listing supported schemes.

---

### D12: Identifier type is chosen by temporal-information exposure risk

**Rule:** Use UUID4 for any entity where a time-sortable ID would expose concrete or relative timing information to an external caller.
Use ULID for everything else.

**Why this matters:** ULIDs and UUID7 embed a millisecond timestamp in the high bits.
If such an ID reaches an external caller — directly via API, indirectly via a log, or through an attacker observing IDs over time — they can extract:

- **Concrete timing**: the exact moment the entity was created
- **Relative ordering**: whether entity A was created before or after entity B

Either signal can be exploitable depending on the entity.
A person-related ID (user, agent, service account) leaking creation time is the clearest case — an attacker could infer when a user signed up, correlate sign-ups across services, or enumerate newly created accounts.
But the same concern applies to any entity whose creation sequence could reveal operational or strategic information.

**Decision for current entities:**

| Entity        | ID type | Rationale                                               |
| ------------- | ------- | ------------------------------------------------------- |
| `Principal`   | UUID4   | Person-related; creation time is private                |
| `Namespace`   | ULID    | Internal workspace handle; temporal sort aids debugging |
| `VersionMeta` | ULID    | Content history; creation order is non-sensitive        |
| `Permission`  | ULID    | Internal ACL record                                     |
| `AuditEvent`  | ULID    | Internal log entry; time-sortable by design             |

**Guidance for future entities:** Ask "if this ID reached an attacker, could the embedded timestamp give them useful information about a person or system?"
If yes, use UUID4.

```python
import uuid
from ulid import ULID

principal_id: str = str(uuid.uuid4())  # fully random — no timing signal
namespace_id: str = str(ULID())  # time-sortable — creation time non-sensitive
version_id: str = str(ULID())
```

The names table stores all identifiers as `TEXT` — no schema distinction needed.

---

### D13: Copy requires no blob duplication

Copy reads the source file's current `VersionMeta` to obtain `content_hash` and `size`, then writes a new file at the destination with the same content hash.
Because blobs are content-addressed, no data is copied — the destination's version record simply references the existing blob.

```python
async def copy(self, ns, src, dst, principal_id):
    # 1. check read on src, write on dst
    # 2. get source version meta
    src_version = await self._meta.get_version(ns, src)
    # 3. put_version at dst with src's content_hash (no blob read/write)
    new_version = VersionMeta(
        content_hash=src_version.content_hash,
        size=src_version.size,
        ...
    )
    await self._meta.put_version(new_version, expected_version=None)  # or CAS if dst exists
```

If the destination already exists, this behaves like a write (new version created).

---

### D14: Move is tombstone + create in a single SQLite transaction

Move is logically: delete source + copy to destination.
For atomicity, both operations are wrapped in a single `aiosqlite` transaction so that a failure leaves neither a partial destination nor an unintended tombstone.

```python
async def move(self, ns, src, dst, principal_id):
    # 1. check read+delete on src, write on dst
    # 2. get source version meta
    async with self._meta.transaction():
        # 3. create tombstone on source
        # 4. create new file/version at destination with source's content_hash
    # 5. audit both operations
```

The `MetadataStore` protocol gains an optional `transaction()` context manager.
SQLite implements it via `aiosqlite`'s `execute("BEGIN")` / `commit()`.
Stores that don't support transactions (future NoSQL) fall back to best-effort sequential operations with a note in their adapter documentation.

---

### D11: `setproctitle` is called at process entry points only

Two call sites:

1. `VFS.initialize()` (when running as a service): `setproctitle("ai-vfs: service")`
2. `GarbageCollector.run()` (when run as a subprocess): `setproctitle("ai-vfs: gc")`

Not called in library mode — `setproctitle` is a side effect that affects the whole
process, so it's only appropriate when ai-vfs owns the process.

---

## Architecture

```text
src/vfs/
├── __init__.py              ← Public API: VFS, VFSConfig, errors
├── config.py                ← VFSConfig (pydantic-settings)
├── models.py                ← FileMeta, VersionMeta, Permission, AuditEvent,
│                              RetentionPolicy, Principal, Namespace, Name,
│                              SearchResult, SearchType
├── errors.py                ← ConflictError, PermissionDeniedError,
│                              NotFoundError
├── protocols/
│   ├── __init__.py
│   ├── metadata.py          ← MetadataStore Protocol
│   ├── blob.py              ← BlobStore Protocol
│   └── search.py            ← SearchProvider Protocol
├── stores/
│   ├── __init__.py
│   ├── sqlite_metadata.py   ← SQLiteMetadataStore
│   ├── local_blob.py        ← LocalFSBlobStore
│   └── cached_blob.py       ← CachedBlobStore
├── search/
│   ├── __init__.py
│   └── default.py           ← DefaultSearchProvider
├── observability/
│   ├── __init__.py
│   ├── tracing.py           ← OTel span helpers, metrics
│   └── audit.py             ← AuditLog helper (thin wrapper over metadata.append_audit_event)
├── vfs.py                   ← VFS orchestrator
└── gc.py                    ← GarbageCollector

tests/
├── conftest.py              ← shared fixtures (tmp_path blob dir, in-memory SQLite, VFS instance)
├── unit/
│   ├── test_models.py
│   ├── test_config.py
│   ├── test_local_blob.py
│   ├── test_cached_blob.py
│   ├── test_sqlite_metadata.py
│   ├── test_default_search.py
│   ├── test_observability.py
│   └── test_gc.py
└── integration/
    ├── test_vfs_file_operations.py
    ├── test_vfs_versioning.py
    ├── test_vfs_access_control.py
    └── test_vfs_search.py
```

## Risks

| Risk                                                     | Mitigation                                                                                                            |
| -------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| SQLite WAL mode contention under concurrent async writes | Use `aiosqlite` connection per coroutine; WAL mode enabled at initialize time (`PRAGMA journal_mode=WAL`)             |
| GC deletes a blob still referenced in a concurrent write | Blob GC checks reference count at delete time; content-addressed puts are idempotent so a re-write after GC is safe   |
| `diskcache` blocking I/O on the asyncio event loop       | `CachedBlobStore` wraps `diskcache` calls in `asyncio.to_thread`                                                      |
| Large test suite slowness from real SQLite/FS            | Unit tests use `:memory:` SQLite and `tmp_path`; integration tests use `tmp_path` with cleanup                        |
| Move partial failure leaves inconsistent state           | Tombstone + create wrapped in a single SQLite transaction (D14); future NoSQL adapters document best-effort semantics |
