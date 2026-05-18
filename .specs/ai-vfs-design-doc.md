# ai-vfs: Virtual Filesystem for AI Agents

**Date:** 2026-04-04 **Status:** Draft **Authors:** ahgraber + Claude

---

## 1. Overview

ai-vfs is a Python library providing virtual filesystem semantics for AI agents.
It separates file content (stored in S3-compatible blob storage) from file metadata (stored in a pluggable database), with per-file versioning, path-based permissions, pluggable search, and sandboxed code execution.

### Goals

- **Library-first SDK** embeddable in any Python agent framework
- **Pluggable storage**: S3-compatible blobs + SQL or NoSQL metadata adapters
- **Per-file versioning** with undo/rollback and Time Machine-style retention
- **Path-based access control** with invisible pruning (default-deny)
- **Sandboxed execution** via [Monty](https://github.com/pydantic/monty) initially, with [Bashkit](https://github.com/everruns/bashkit) as a future shell provider and [just-bash](https://github.com/vercel-labs/just-bash) as a tertiary JS/TS integration
- **Self-hostable** with sensible local defaults (SQLite + local filesystem)

### Non-Goals (for this spec)

- Unified agent namespace (tools-as-files, memory-as-files) — consumer-layer concern
- Service/API wrapper — future work on top of the library
- CRDT-based collaborative editing — future horizon
- Git-like branching/merging of workspaces

### Key Design Decisions

| Decision           | Choice                                                                     | Rationale                                                                                                           |
| ------------------ | -------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| Architecture       | Custom VFS with [fsspec](https://github.com/fsspec/filesystem_spec) bridge | Clean domain model; versions, permissions, content-addressing are first-class rather than bolted onto fsspec        |
| Content addressing | [BLAKE3](https://github.com/BLAKE3-team/BLAKE3)                            | Cryptographically secure, ~2x faster than SHA-256, deduplication for free                                           |
| Concurrency        | Optimistic (CAS via version stamps)                                        | No locks, no coordination layer; borrowed from [turbopuffer's S3 pattern](https://turbopuffer.com/blog/turbopuffer) |
| Identifiers        | ULIDs (file/metadata entities) + UUID4 (person-related entities)           | ULID for temporal sortability in logs; UUID4 for principals to avoid leaking creation-time ordering                 |
| Metadata adapter   | Abstract protocol (~10 methods)                                            | Supports SQL (SQLite, Postgres) and NoSQL (Mongo, Cosmos) without ORM coupling                                      |
| Execution          | Pluggable provider protocol                                                | Start with Monty external functions; add Bashkit later, then evaluate just-bash for JS/TS environments              |
| Config             | pydantic-settings                                                          | Validated, environment-aware, typed configuration                                                                   |

### Prior Art Decision Map

The appendix lists broader references.
These are the prior-art links that directly shape ai-vfs decisions:

| Decision surface           | Prior art                                                                                | Effect on ai-vfs                                                                                                    |
| -------------------------- | ---------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| VFS boundary               | LangChain Deep Agents backends, AGFS, fsspec                                             | Keep a small native VFS contract and defer fsspec to an adapter rather than making fsspec the domain model          |
| Access pruning             | Mintlify ChromaFS, Leonie Monigatti's Elasticsearch VFS                                  | Enforce permissions before listing, search, and result rendering so unauthorized paths are invisible                |
| Search acceleration        | Mintlify ChromaFS, Cursor fast regex search                                              | Use coarse-provider filtering plus content verification; indexes improve speed but never become the correctness API |
| Search artifact lifecycle  | Cursor-style local indexes, vector-backend patterns from agent filesystem writing        | Store provider artifacts behind a standard `SearchArtifact` envelope while leaving payloads provider-defined        |
| Execution interface        | Cloudflare Code Mode, Anthropic code execution with MCP, Monty, Bashkit, just-bash       | Prefer code-mode orchestration over many tiny tools; expose curated VFS operations first, then shell syntax later   |
| Token-efficient editing    | Anthropic MCP filesystem organization, Dirac hash anchors with Myers diff reconciliation | Return compact, anchored read output and edit by anchors so large changes do not repeat unchanged text              |
| Persistent agent workspace | AIGNE, "Agent OS is a Filesystem", managed-agent persistence patterns                    | Treat files, versions, search artifacts, and execution state as persistent agent context rather than transient logs |

---

## 2. Core Domain Model

### 2.1 Entities

**Namespace** — an isolated workspace.
All paths are relative to a namespace.
The isolation boundary for permissions.
Maps to a prefix in blob storage and a partition key in the metadata store.

```text
Namespace:
  id: ULID
  display_name: str               # human-friendly, looked up via names table
  created_at: datetime
  created_by: UUID4               # principal
  retention_policy: RetentionPolicy | None   # override global default
```

**File** — a path within a namespace pointing to versioned content.

```text
File:
  namespace_id: ULID
  path: str                       # e.g., "/workspace/main.py"
  current_version_id: ULID
  current_version_number: int
  created_at: datetime
  updated_at: datetime
  is_deleted: bool                # true if current version is a tombstone
```

**Version** — an immutable snapshot of a file at a point in time.

```text
Version:
  id: ULID
  file_path: str
  namespace_id: ULID
  version_number: int             # per-file monotonic counter (human-facing)
  content_hash: str               # BLAKE3 → blob store key
  size: int                       # bytes
  created_at: datetime
  created_by: UUID4               # principal
  is_tombstone: bool              # true for deletes
  search_meta: SearchMeta         # derived artifact manifest; provider_key → SearchArtifact
  parent_version_id: ULID | None  # for rollbacks, points to the version this was rolled back from
```

**Principal** — an identity (agent, user, service) that accesses the VFS.

```text
Principal:
  id: UUID4                       # fully random — no embedded timestamp; prevents
                                  # inferring creation order of principals from IDs
  display_name: str
  principal_type: str             # "agent", "user", "service"
  created_at: datetime
```

**Names table** — maps identifiers to human-friendly display names for all entity types.

```text
Name:
  entity_type: str                # "namespace", "principal", "permission"
  entity_id: str                  # ULID for file-system entities; UUID4 for principals
  display_name: str
```

Identifier fields use the identifier type of the entity they point to.
For example, `Permission.id` is a ULID because the permission record is internal metadata, while `Permission.principal_id` is a UUID4 because it references a principal.

### 2.2 Operations

| Operation  | Input                          | Output                         | Storage access             |
| ---------- | ------------------------------ | ------------------------------ | -------------------------- |
| `stat`     | path                           | file metadata (no content)     | metadata only              |
| `read`     | path, version_number?          | bytes                          | metadata + blob            |
| `write`    | path, bytes, expected_version? | version ID + version_number    | blob put + metadata insert |
| `delete`   | path                           | tombstone version ID           | metadata only              |
| `list`     | path prefix, recursive?        | list of file metadata          | metadata only              |
| `search`   | query, scope, search_type      | matching paths + context lines | metadata + optional blob   |
| `versions` | path, limit?, before?          | version history                | metadata only              |
| `rollback` | path, target_version_number    | new version ID                 | metadata (+ blob ref)      |

**Design principles:**

- **Lazy content**: `list`, `stat`, `versions` never touch the blob store.
- **Delete is a tombstone**: Creates a special version marker.
  Old versions remain accessible.
  GC reclaims them per retention policy.
- **Rollback creates a new version**: Rolling back to version 3 creates version N+1 whose `content_hash` points to version 3's blob.
  History is append-only; no version is ever mutated.
- **Optimistic concurrency**: `write` accepts an optional `expected_version`.
  If provided, the write fails with a conflict error if the current version doesn't match.
  Callers retry.
  If omitted, last-writer-wins (both versions are preserved).

---

## 3. Layer Architecture

```text
+---------------------------------------------+
|  Consumer Layer                              |
|  (agent frameworks, CLI, fsspec bridge)      |
|  Uses: VFS public API                        |
+---------------------------------------------+
|  VFS Layer                                   |
|  Orchestrates metadata + blob + search       |
|  Enforces: permissions, versioning,          |
|            concurrency, retention            |
|  Emits: OTel spans + audit log entries       |
+------+--------+-----------+------------------+
| Meta | Blob   | Search    | Execution        |
| Store| Store  | Provider  | Provider         |
| Proto| Proto  | Protocol  | Protocol         |
+------+--------+-----------+------------------+
|SQLite| S3     | Glob/Grep | Monty            |
|Postgr| MinIO  | Bloom     | Bashkit          |
|Mongo | Azure  | Semantic  | Eryx             |
|      | LocalFS| (plugin)  | (plugin)         |
+------+--------+-----------+------------------+
```

Between the Execution Provider and the VFS Layer sits the **Shell Operations Layer** —
thin wrappers that expose a curated set of bash-familiar functions over VFS operations:

```text
Execution Provider (Monty initially; Bashkit later)
    |
    |  sandbox calls: grep("pattern", "/workspace/", recursive=True)
    v
Shell Operations Layer
    |  translates to: vfs.search(query="pattern", scope="/workspace/", type=regex)
    v
VFS Layer
```

### 3.1 Metadata Store Protocol

```python
class MetadataStore(Protocol):
    # File operations
    async def put_file(self, namespace_id: ULID, path: str, file: FileMeta) -> None: ...
    async def get_file(self, namespace_id: ULID, path: str) -> FileMeta | None: ...
    async def delete_file(self, namespace_id: ULID, path: str) -> None: ...
    async def list_dir(self, namespace_id: ULID, path_prefix: str, recursive: bool = False) -> list[FileMeta]: ...

    # Version operations (all mutations use CAS via expected_version)
    async def put_version(self, version: VersionMeta, expected_version: int | None = None) -> None: ...
    async def get_version(
        self, namespace_id: ULID, path: str, version_number: int | None = None
    ) -> VersionMeta | None: ...
    async def list_versions(
        self, namespace_id: ULID, path: str, limit: int = 50, before: ULID | None = None
    ) -> list[VersionMeta]: ...

    # Permissions
    async def check_permission(self, principal_id: UUID4, namespace_id: ULID, path: str, operation: str) -> bool: ...
    async def set_permission(self, permission: Permission) -> None: ...

    # Audit
    async def append_audit_event(self, event: AuditEvent) -> None: ...

    # Search metadata
    async def get_search_meta_batch(self, version_ids: list[ULID]) -> dict[ULID, SearchMeta]: ...
    async def update_search_artifact(self, version_id: ULID, provider_key: str, artifact: SearchArtifact) -> None: ...

    # Names
    async def set_name(self, entity_type: str, entity_id: str, display_name: str) -> None: ...
    async def resolve_name(self, entity_type: str, display_name: str) -> str | None: ...

    # GC (namespace_id=None means scan all namespaces)
    async def list_reclaimable_versions(
        self, policy: RetentionPolicy, namespace_id: ULID | None = None
    ) -> list[VersionMeta]: ...
    async def delete_versions(self, version_ids: list[ULID]) -> None: ...
```

All mutations that depend on current state accept an `expected_version` parameter.
Implementation handles CAS semantics:

- SQL: `UPDATE ... WHERE version_number = ?` (raises `ConflictError` on 0 rows affected)
- Mongo: `find_one_and_update` with version match
- S3-CAS: conditional PUT with `If-Match`

### 3.2 Blob Store Protocol

```python
class BlobStore(Protocol):
    # Whole-object operations (initial implementation)
    async def put(self, content_hash: str, data: bytes) -> None: ...
    async def get(self, content_hash: str) -> bytes: ...
    async def delete(self, content_hash: str) -> None: ...
    async def exists(self, content_hash: str) -> bool: ...

    # Streaming operations (for large files; initially raise NotImplementedError)
    async def put_stream(self, content_hash: str, stream: AsyncIterator[bytes]) -> None: ...
    async def get_stream(self, content_hash: str) -> AsyncIterator[bytes]: ...
```

Blobs are immutable and content-addressed.
`put` is idempotent — if the hash already exists, it's a no-op.
`delete` is called only by GC.
The blob store has no concept of files, paths, or versions.

**Caching:** The VFS wraps the blob store in an optional [`diskcache`](https://github.com/grantjenks/python-diskcache)-backed caching layer.
Enabled by default for remote blob stores (S3, Azure), disabled for local FS.
Cache is keyed by content hash — immutable blobs make invalidation trivial (never needed).
Cache eviction is LRU with a configurable max size.

### 3.3 Search Provider Protocol

```python
ProviderKey = str
SearchMeta = dict[ProviderKey, "SearchArtifact"]


@dataclass(frozen=True)
class SearchArtifact:
    status: Literal["ready", "failed", "unsupported"]
    schema_version: int
    provider_key: ProviderKey
    provider_version: str | None
    params_hash: str
    content_hash: str
    created_at: datetime

    storage: Literal["inline", "blob", "external"]
    payload: dict | None = None
    artifact_ref: str | None = None

    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class SearchMetaEntry:
    path: str
    file: FileMeta
    version: VersionMeta
    search_meta: SearchMeta


ContentReader = Callable[[str], Awaitable[bytes]]


@dataclass(frozen=True)
class SearchLimits:
    max_content_reads: int | None = None
    cwd: str | None = None


@dataclass(frozen=True)
class SearchRequest:
    query: str
    scope: str
    search_type: SearchType
    search_metas: Iterable[SearchMetaEntry]
    read_content: ContentReader
    limits: SearchLimits


@dataclass(frozen=True)
class SearchResponse:
    results: list[SearchResult]
    scope_narrowed: bool = False
    actual_scope: str | None = None
    total_files_in_scope: int | None = None


class SearchProvider(Protocol):
    def provider_key(self) -> ProviderKey: ...
    async def index(self, path: str, content: bytes, version: VersionMeta) -> SearchArtifact | None: ...
    async def search(self, request: SearchRequest) -> SearchResponse: ...
    def capabilities(self) -> set[SearchType]: ...
```

`SearchType` is an enum: `glob`, `find`, `regex`, `fulltext`, `semantic`.

Multiple providers can be active simultaneously.
The VFS dispatches a search to the provider that declares the matching capability.
If multiple providers match, the VFS uses the most specific (e.g., bloom-accelerated regex over brute-force regex).

The VFS owns the safety boundary for every search request:

- resolves the requested scope against the caller's namespace
- filters to paths and current versions visible to the principal
- builds permission-pruned `SearchMetaEntry` values
- injects a `read_content` callback that enforces the same authorization,
  version, timeout, and rate-limit checks
- fills response metadata such as `scope_narrowed`, `actual_scope`, and
  `total_files_in_scope`

The provider owns the search strategy inside that envelope.
A provider may ignore `search_meta` and brute-force through `read_content`, use `search_meta` as a coarse filter before verification, or rank metadata-only artifacts such as embeddings.

`index()` is called synchronously during `write`.
Returns a `SearchArtifact` to store at `search_meta[provider_key]`.
For the default provider (glob/find only), this is a no-op.
The `SearchArtifact` envelope is standard; its `payload` is provider-defined.

**Dependency note:** Search providers do not access the metadata or blob stores directly.
They receive VFS-scoped metadata entries and a controlled content reader.
Providers are storage-independent and search has no direct side effects on VFS state.

### 3.4 Execution Provider Protocol

```python
class ExecutionProvider(Protocol):
    async def execute(
        self,
        code: str,
        fs_ops: FsOperations,
        timeout: float | None = None,
        resource_limits: ResourceLimits | None = None,
    ) -> ExecutionResult: ...

    def capabilities(self) -> ExecutionCapabilities: ...
    async def reset(self) -> None: ...
```

`FsOperations` — the bridge between sandbox and VFS:

```python
@dataclass
class FsOperations:
    # Session state operations (Session-backed; see `shell-context` change).
    # `cd` is async because it triggers a permission check; `pwd` is pure state.
    cd: Callable[[str], Awaitable[None]]
    pwd: Callable[[], str]

    # Core VFS operations (injected as sandbox callables). All async — they touch
    # the metadata/blob stores via the bound Session.
    read: Callable[[str], Awaitable[bytes]]
    write: Callable[[str, bytes], Awaitable[str]]
    list: Callable[[str, bool], Awaitable[list[dict]]]
    stat: Callable[[str], Awaitable[dict]]
    delete: Callable[[str], Awaitable[None]]

    # Shell operation wrappers (async; wrap the core ops).
    grep: Callable[..., Awaitable[list[dict]]]
    find: Callable[..., Awaitable[list[str]]]
    glob: Callable[[str], Awaitable[list[str]]]
    head: Callable[[str, int], Awaitable[bytes]]
    tail: Callable[[str, int], Awaitable[bytes]]
    edit: Callable[..., Awaitable[dict]]
```

`FsOperations` is constructed against a `Session` (see `changes/shell-context/` while active, then `changes/archive/<date>-shell-context/` after archival) rather than directly against `VFS`.
The Session binds `namespace_id`, `principal_id`, and an in-memory `cwd`, so every callback in `FsOperations` resolves relative paths through `cwd` and inherits the principal's permission scope.
Most adapters need a real `async def` (not a lambda) because they `await` the Session call and post-process the result:

```python
def fs_operations_for(session: Session) -> FsOperations:
    async def _write(path: str, data: bytes) -> str:
        version = await session.write(path, data)
        return version.id

    async def _list(path: str, recursive: bool) -> list[dict]:
        entries = await session.list(path, recursive=recursive)
        return [m.model_dump() for m in entries]

    async def _stat(path: str) -> dict:
        meta = await session.stat(path)
        return meta.model_dump()

    async def _delete(path: str) -> None:
        await session.delete(path)  # VersionMeta discarded — FsOperations exposes fire-and-forget delete

    return FsOperations(
        cd=session.cd,  # Session methods that already match the FsOperations signature bind directly
        pwd=session.pwd,
        read=session.read,
        write=_write,
        list=_list,
        stat=_stat,
        delete=_delete,
        grep=...,  # shell wrappers below — see § 3.5
        ...,
    )
```

This keeps three concerns cleanly factored: VFS owns storage + permission gates, Session owns CWD + relative-path resolution, `FsOperations` owns bash naming + anchor management.
The sandbox can only reach what the principal is allowed to access, and it observes a coherent bash-style filesystem (`cd`, `pwd`, `./relative`) without any of that state leaking into VFS.

```python
@dataclass
class ResourceLimits:
    timeout_seconds: float = 30.0
    max_memory_bytes: int | None = None
    max_operations: int = 1000  # cap on VFS callbacks per execution
```

`max_operations` is VFS-level rate limiting.
The execution provider enforces its own internal limits (Monty memory/allocation/time, future Bashkit command/parser/filesystem limits, Eryx WASM fuel).

### 3.5 Shell Operations Layer

Wrappers that expose a curated, bash-familiar function set over VFS operations.
These are injected into Monty as explicit external functions.
They are not a POSIX compatibility layer; Bashkit can provide real shell syntax later.

Path-taking wrappers accept relative or absolute paths.
Relative paths resolve through the bound Session's `cwd`; the wrapper itself does no path math.

| Wrapper                                                                    | Bash equivalent                     | Dispatch                                                       |
| -------------------------------------------------------------------------- | ----------------------------------- | -------------------------------------------------------------- |
| `cd(path)`                                                                 | `cd /path/`                         | `session.cd(path)` (resolves, checks read perm, updates `cwd`) |
| `pwd()`                                                                    | `pwd`                               | `session.pwd()`                                                |
| `grep(pattern, path, recursive=True)`                                      | `grep -r "pattern" /path/`          | search provider (bloom/regex) or fallback to read + match      |
| `find(path, name=None, size=None)`                                         | `find /path -name "*.py" -size +1k` | metadata store queries                                         |
| `glob(pattern)`                                                            | `ls /path/*.py`                     | path-pattern match against metadata                            |
| `cat(path)`                                                                | `cat /path/file`                    | `session.read(path)`                                           |
| `ls(path, all=False, long=False)`                                          | `ls -la /path/`                     | `session.list(path)` with structured metadata                  |
| `head(path, n=10)`                                                         | `head -n 10 /path/file`             | `session.read(path)` + slice                                   |
| `tail(path, n=10)`                                                         | `tail -n 10 /path/file`             | `session.read(path)` + slice                                   |
| `edit(path, start_anchor, end_anchor, replacement, expected_version=None)` | `apply patch to anchored range`     | validate anchors, then `session.write(path)`                   |

The shell ops layer enables the [Mintlify/ChromaFS](https://www.mintlify.com/blog/how-we-built-a-virtual-filesystem-for-our-assistant) optimization pattern:
`grep` hits the search index first as a coarse filter, then verifies matches
against actual content as a fine filter.

### 3.6 Token-Efficient Anchored Editing

Monty read-like commands can expose editable text with compact, session-scoped anchors.
This follows the [Dirac hash-anchor pattern](https://dirac.run/posts/hash-anchors-myers-diff-single-token#token-efficiency): the model reads anchored lines, then edits by naming start/end anchors plus replacement text instead of repeating the old text.
For large edits this reduces edit-call output from `O(old_text + replacement)` to `O(replacement)`.

Anchors are a Shell Operations concern, not VFS metadata.
The shell ops layer maintains an execution/session-scoped anchor map for files read through `cat`, `head`, `tail`, `grep`, or an explicit editable read mode.
The map binds compact anchors to `(namespace_id, path, version_number, line_content)` entries.
Anchors should be single-token when available, falling back to longer anchors only after the single-token pool is exhausted for the current session.

`edit(path, start_anchor, end_anchor, replacement, expected_version=None)` validates that:

- the anchors belong to the requested path and current session
- the file version still matches `expected_version` when provided
- the current line content still matches the anchored lines

After a successful edit, the shell ops layer reconciles the old and new line sequences with Myers diff.
Unchanged lines keep their anchors; changed or inserted lines receive new anchors.
The edit response returns the new version number plus updated anchors for the affected range.

If anchor validation fails, the command fails closed with a conflict result and asks the agent to reread the affected file.
No stale anchor should ever silently apply to a different line.

---

## 4. Versioning & Retention

### 4.1 Version Model

Every `write` produces a new `Version` record.
Versions are immutable and append-only.

- `version_number`: per-file monotonic integer (human-facing: "rollback to version 3")
- `id` (ULID): globally unique, time-sortable (internal)
- `content_hash` (BLAKE3): key into the blob store

Multiple versions can share the same `content_hash` (deduplication).
A rollback from version 7 to version 3 creates version 8 with `content_hash` equal to version 3's hash — no blob copy, just a new metadata row.

### 4.2 Retention Policy (Time Machine Model)

Configurable per-namespace, with global defaults:

```python
@dataclass
class RetentionPolicy:
    max_recent_versions: int = 50  # keep the N most recent
    tiers: list[RetentionTier] = field(
        default_factory=lambda: [
            RetentionTier(max_age=timedelta(hours=24), keep_every=None),  # all
            RetentionTier(max_age=timedelta(days=7), keep_every=timedelta(hours=1)),
            RetentionTier(max_age=timedelta(days=30), keep_every=timedelta(days=1)),
            RetentionTier(max_age=timedelta(days=365), keep_every=timedelta(weeks=1)),
        ]
    )
    keep_first_version: bool = True  # always keep version 1
    keep_current_version: bool = True  # always keep the latest
```

Tiers are evaluated in order.
A version is reclaimable if it falls within a tier's age range and there's a newer version within that tier's `keep_every` interval.

### 4.3 Garbage Collection

GC runs as a background process (scheduled or manual), two phases:

1. **Version GC**: Apply retention policy per file.
   Mark expired versions as reclaimable.
   Delete version metadata rows.
2. **Blob GC**: Find `content_hash` values with zero remaining references across all namespaces and all versions.
   Delete from blob store.

Blob GC must be conservative — a content hash may be referenced by versions in different namespaces (content-addressed = shared).
Only delete when global reference count = 0.

GC is safe to skip indefinitely.
The system accumulates versions and blobs until GC runs.
No correctness dependency on GC.

---

## 5. Permissions

### 5.1 Model

```text
Permission:
  id: ULID
  principal_id: UUID4
  namespace_id: ULID
  path_prefix: str              # default "/" = entire namespace
  operations: set[str]          # {"read", "write", "delete", "execute", "admin"}
```

### 5.2 Enforcement Rules

- **Default-deny**: A principal with no matching permission is denied.
- **Most-specific-first**: `/workspace/drafts/` overrides `/workspace/`.
- **Invisible pruning**: `list` and `stat` exclude paths the principal cannot read.
  The agent cannot reference or discover unauthorized paths.
- **Namespace isolation**: Cross-namespace access requires an explicit permission entry.
  The VFS never leaks metadata across namespace boundaries — even `search` is scoped.
- **Admin**: Grants permission management on that subtree.

### 5.3 Future RBAC Expansion

The schema supports expansion without migration:

- Add a `role` field to permissions (`owner`, `editor`, `viewer`, custom)
- Roles map to operation sets: `viewer = {read}`, `editor = {read, write}`, `owner = {read, write, delete, admin}`
- `principal_id` can reference a group for team-level permissions

---

## 6. Observability & Audit

### 6.1 OpenTelemetry Instrumentation

Every VFS operation is a span:

```text
Span: vfs.write
  Attributes:
    vfs.namespace_id: "01JQX..."
    vfs.path: "/workspace/main.py"
    vfs.principal_id: "550e8400-e29b-41d4-a716-446655440000"
    vfs.version_number: 4
    vfs.content_hash: "b3_a1b2c3..."
    vfs.blob_size_bytes: 1234
  Children:
    metadata.get_file (duration: 2ms)
    blob.put (duration: 45ms)
    search.index (duration: 12ms)
    metadata.put_version (duration: 3ms)
    audit.append (duration: 1ms)
```

Metrics:

- `vfs.operation.count` (by operation, namespace)
- `vfs.operation.duration` (histogram, by operation)
- `vfs.blob.size` (histogram)
- `vfs.search.candidates` (histogram — coarse filter results before verification)

The agent framework's traces can parent-link to VFS spans via standard OTel context propagation.

### 6.2 Audit Log

Stored in the metadata DB.
Append-only.

```text
AuditEvent:
  event_id: ULID
  timestamp: datetime
  namespace_id: ULID
  principal_id: UUID4
  operation: str              # "write", "delete", "rollback", "permission_change", "gc_run"
  path: str | None
  version_id: ULID | None
  detail: dict                # operation-specific: old/new content_hash, permission diff, etc.
  trace_id: str | None        # OTel trace ID for correlation
```

**What gets audited:**

- All writes, deletes, rollbacks (state changes)
- Permission changes
- GC runs (which versions/blobs were reclaimed)

**What does not get audited** (OTel spans only):

- Reads — too noisy for the audit log

**Correlation:** Audit events carry the OTel `trace_id` so you can jump from
"this file was modified at 3pm" to the full execution trace.

---

## 7. Search

### 7.1 Built-in Search (Default Provider)

Handles path-based operations with no indexing overhead:

- **Glob**: `*.py`, `workspace/**/*.md` — metadata-only path matching
- **Find**: predicate-based metadata search (name patterns, size, mtime, type)
- **Grep**: regex or literal match against file content — requires blob reads

Grep without an acceleration provider uses the default provider: the VFS enumerates the permission-pruned scope, injects `read_content`, and the provider reads and matches each file.
Correct but O(n) in visible file count.

### 7.2 Bloom Filter Provider (Plugin)

- **On `index`**: Computes bloom filter hashes (xxhash-based) for file content.
  Stores a ready `SearchArtifact` at a provider key such as `search_meta["bloom/default"]`.
  The artifact uses the standard envelope; bloom hashes, masks, normalizer IDs, and similar details live in the provider-defined payload.
- **On `search(regex)`**: Receives permission-pruned `SearchMetaEntry` values,
  tests bloom filters across the visible files in scope, and treats missing or
  corrupt bloom data as conservative candidates.
- **Verification**: Calls `read_content` only for candidate files and verifies
  actual content before returning matches.
- Reduces grep from O(n) blob reads to O(k) where k \<< n.

Potential integration with [ahgraber/bloom-search](https://github.com/ahgraber/bloom-search) or
a [Cursor-style bloom extension](https://cursor.com/blog/fast-regex-search) for regex-capable indexing.

### 7.3 Semantic Search Provider (Future Plugin)

- **On `index`**: Computes embedding vector.
  Stores a ready `SearchArtifact` at a provider key such as `search_meta["semantic/local-minilm"]`.
- **On `search(semantic)`**: Ranks permission-pruned semantic artifacts by similarity to the query embedding.
  Small/local providers may store vectors inline in `payload`.
  Larger providers should store `artifact_ref` values that point to blob-backed vectors or provider-owned external indexes.
- Backend options: pgvector (Postgres), Atlas Search (Mongo), external vector DB.
  Any external vector backend must preserve the same VFS boundary: authorization, scope, and visibility are resolved before the provider can rank or fetch content.

### 7.4 Search Dispatch

```text
search(query, scope, type)
    |
    v
VFS: authorize caller, resolve scope, enumerate visible current versions
    |
    v
VFS: select provider, build SearchRequest(search_metas, read_content, limits)
    |
    v
provider.search(request)
    |
    +-- default provider: read_content for each visible file
    |
    +-- bloom provider: search_meta coarse filter, read_content verification
    |
    +-- semantic provider: rank semantic SearchArtifact entries
    |
    v
SearchResponse(results, scope_narrowed, actual_scope, total_files_in_scope)
```

The agent doesn't know or care which path was taken.
Monty receives the same `grep(...)` function either way.
Bashkit later maps `grep -r pattern path` to the same shell operation.

### 7.5 Search Metadata Storage

Search artifacts are stored per-version in a `search_meta` manifest (JSONB in SQL, nested document in NoSQL).
The manifest shape is standard: provider keys map to `SearchArtifact` envelopes.
The envelope carries lifecycle and freshness fields that the VFS can reason about generically.
Each provider owns only the `payload` schema or external artifact referenced by `artifact_ref`.

`search_meta` is a derived attachment to a version, not part of the immutable content identity.
Adapters may physically embed it on the version record for simplicity, but they must treat artifact updates as version-scoped derived metadata keyed by `version_id + provider_key`.

Required metadata-store behavior is limited to storing, updating, and batch loading `SearchArtifact` envelopes for VFS-visible current versions.
Metadata adapters do not need to understand bloom filters, embedding vectors, or provider-specific query semantics.
Provider-specific accelerated indexes may be added later, but they must remain behind the provider boundary and the VFS authorization/scope filter.

---

## 8. Execution

### 8.1 Tiered Model

| Tier     | Provider                                               | Language      | Startup           | Security                                  | Use case                         |
| -------- | ------------------------------------------------------ | ------------- | ----------------- | ----------------------------------------- | -------------------------------- |
| Initial  | [Monty](https://github.com/pydantic/monty)             | Python subset | In-process, \<1us | Rust VM, explicit external functions only | Code-mode orchestration over VFS |
| Future   | [Bashkit](https://github.com/everruns/bashkit)         | Bash          | In-process        | Rust virtual bash, no host I/O by default | Shell syntax and command scripts |
| Tertiary | [just-bash](https://github.com/vercel-labs/just-bash)  | Bash-like TS  | In-process / VM   | TypeScript interpreter, no shell process  | JS/TS ecosystem integration      |
| Future   | [Eryx](https://github.com/eryx-org/eryx)               | Full CPython  | ~16ms (AOT)       | WASM sandbox, all 6 vectors blocked       | Complex Python, stdlib           |
| Future   | [PyMiniRacer](https://github.com/bpcreech/PyMiniRacer) | JavaScript    | In-process        | V8 isolate                                | JS/TS execution                  |
| Future   | [E2B](https://e2b.dev)/sidecar                         | Any           | ~150ms            | Firecracker microVM                       | Pip packages, full OS            |

Bashkit is not deferred because it requires a Node sidecar.
Current Bashkit docs describe an in-process Rust virtual bash with Python bindings via PyO3 and JavaScript/TypeScript bindings via NAPI-RS.
It is deferred because real shell syntax, parsing, and command semantics expand the execution contract beyond the initial Monty callback model.
Vercel's just-bash is the Node/TypeScript bash-like runtime previously considered as a sidecar-style option.
It remains a tertiary follow-on after Monty and Bashkit because it primarily serves JS/TS agent stacks.

### 8.2 Integration Pattern

The initial Monty provider uses the **function-injection pattern**:

1. VFS constructs `FsOperations` bound to the calling principal/namespace
2. Shell ops layer wraps VFS operations in bash-familiar signatures
3. Monty receives `FsOperations` as explicit external functions
4. Sandboxed code calls these functions — they route back through the VFS
5. The sandbox never accesses storage directly

`SearchRequest`, `search_meta`, and `read_content` remain internal to the VFS and search-provider boundary.
Monty sees only the curated shell functions.
Bashkit later exposes shell syntax for the same operations without changing the underlying search contract.

```text
Agent code: grep("TODO", "/workspace/", recursive=True)
    |
    v
Monty sandbox --> shell_ops.grep("TODO", "/workspace/", recursive=True)
    |
    v
VFS.search(query="TODO", scope="/workspace/", type=regex)
    |
    v
SearchRequest(search_metas, read_content) --> BloomProvider.search() --> SearchResponse
```

### 8.3 Resource Limits

Enforced at two levels:

- **VFS level**: `max_operations` caps the number of VFS callbacks per execution
  (prevents unbounded read loops)
- **Provider level**: Each provider enforces its own limits (Monty memory/allocation/time,
  future Bashkit command/parser/filesystem limits, Eryx WASM fuel, timeout)

---

## 9. Configuration

### 9.1 pydantic-settings Config

```python
from pydantic_settings import BaseSettings


class VFSConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AIFS_")

    # Storage
    metadata_store_uri: str = "sqlite:///./aifs.db"
    blob_store_uri: str = "file:///./aifs_blobs/"

    # Versioning & retention
    retention_max_recent: int = 50
    retention_tiers: list[dict] | None = None  # override default Time Machine tiers

    # Observability
    otel_enabled: bool = True
    audit_log_enabled: bool = True

    # Search providers (list of registered provider names)
    search_providers: list[str] = ["default"]

    # Execution providers (list of registered provider names)
    execution_providers: list[str] = []

    # Resource limits
    default_timeout_seconds: float = 30.0
    default_max_operations: int = 1000
```

### 9.2 URI-Based Store Resolution

Store URIs are resolved to adapter implementations at construction:

| URI scheme           | Adapter               |
| -------------------- | --------------------- |
| `sqlite:///path`     | SQLiteMetadataStore   |
| `postgresql://...`   | PostgresMetadataStore |
| `mongodb://...`      | MongoMetadataStore    |
| `s3://bucket/prefix` | S3BlobStore           |
| `file:///path`       | LocalFSBlobStore      |

### 9.3 Constructor

```python
from ai_vfs import VFS

# Minimal — SQLite + local filesystem, no plugins
vfs = VFS()

# Self-hosted — S3 + Postgres, bloom search, bashkit execution
vfs = VFS(
    metadata_store="postgresql://localhost/aifs",
    blob_store="s3://my-bucket/aifs",
    search_providers=["bloom"],
    execution_providers=["bashkit"],
)
```

### 9.4 Deployment Profiles

| Profile     | Metadata | Blobs    | Search              | Execution              |
| ----------- | -------- | -------- | ------------------- | ---------------------- |
| Local dev   | SQLite   | Local FS | Default (glob/grep) | None                   |
| Self-hosted | Postgres | MinIO/S3 | Bloom               | Monty                  |
| Production  | Postgres | S3       | Bloom + Semantic    | Monty + Bashkit + Eryx |
| JS/TS agent | Postgres | S3       | Bloom + Semantic    | Monty + just-bash      |

### 9.5 Secrets

All credentials (S3 keys, DB connection strings) via environment variables or `.env` file (gitignored).
Never in config objects or committed files.

### 9.6 Process Identification

When running as a service or background GC process, ai-vfs sets `setproctitle("ai-vfs: <role>")` per project conventions.

---

## 10. Public API Surface

```python
from ai_vfs import VFS

# Lifecycle
vfs = VFS(config=VFSConfig(...))  # or VFS() for defaults
await vfs.initialize()  # create tables, verify blob store access
await vfs.close()  # cleanup connections

# Namespace management
ns = await vfs.create_namespace(display_name="my-workspace")
await vfs.grant(principal_id, ns.id, path_prefix="/", operations={"read", "write"})

# File operations (all scoped to a principal + namespace)
meta = await vfs.stat(ns.id, "/main.py", principal=p)
data = await vfs.read(ns.id, "/main.py", principal=p, version_number=3)
ver = await vfs.write(ns.id, "/main.py", content, principal=p, expected_version=4)
await vfs.delete(ns.id, "/main.py", principal=p)
files = await vfs.list(ns.id, "/src/", principal=p, recursive=True)

# Versioning
history = await vfs.versions(ns.id, "/main.py", principal=p, limit=20)
ver = await vfs.rollback(ns.id, "/main.py", target_version=3, principal=p)

# Search
results = await vfs.search("TODO", scope="/src/", namespace=ns.id, principal=p, search_type="regex")

# Execution
result = await vfs.execute(
    code='grep -r "TODO" /workspace/',
    namespace=ns.id,
    principal=p,
    provider="bashkit",
)

# GC (manual trigger; also runnable on a schedule)
gc_result = await vfs.run_gc()

# Reindex (backfill search metadata for a provider added after files were written)
await vfs.reindex(namespace=ns.id, provider="bloom", scope="/src/")
```

---

## 11. Dependencies (Initial)

| Dependency          | Purpose                 | Notes                                |
| ------------------- | ----------------------- | ------------------------------------ |
| `blake3`            | Content hashing         | Rust-backed, PyPI                    |
| `python-ulid`       | ID generation           | Pure Python                          |
| `pydantic`          | Domain models           | Already in ecosystem                 |
| `pydantic-settings` | Configuration           | Env-aware config                     |
| `opentelemetry-api` | Tracing/metrics         | Instrumentation only (no SDK forced) |
| `aiosqlite`         | SQLite metadata adapter | Async SQLite                         |
| `aiofiles`          | Local FS blob adapter   | Async file I/O                       |
| `diskcache`         | Blob caching layer      | LRU cache for remote blob stores     |

**Optional (per deployment):**

| Dependency             | Purpose                     |
| ---------------------- | --------------------------- |
| `asyncpg`              | Postgres metadata adapter   |
| `motor`                | Mongo metadata adapter      |
| `s3fs` / `aiobotocore` | S3 blob adapter             |
| `bashkit-python`       | Bash execution provider     |
| `pydantic-monty`       | Python execution provider   |
| `fsspec`               | fsspec compatibility bridge |

---

## 12. Resolved Design Questions

1. **fsspec bridge**: Deferred.
   Not in initial scope.
   The native VFS API is the primary interface; an fsspec `AbstractFileSystem` adapter can be added later as a compatibility convenience.

2. **Streaming reads/writes**: Include provisions in the protocol.
   The `BlobStore` protocol includes both `put(hash, bytes)` / `get(hash) → bytes` for simple cases and `put_stream(hash, AsyncIterator[bytes])` / `get_stream(hash) → AsyncIterator[bytes]` for large files.
   The VFS `read`/`write` API exposes both `bytes` and streaming variants.
   Implementation: start with `bytes`-only in initial adapters; streaming methods raise `NotImplementedError` until needed.

3. **Blob store caching**: Built-in via `diskcache`.
   The VFS includes an optional local cache layer (disabled by default for local-FS blobs, enabled by default for remote blob stores like S3).
   Cache is keyed by content hash — immutable blobs make cache invalidation trivial (never needed).

4. **Search metadata schema evolution**: Two mechanisms:

   - **Lazy backfill**: When a search encounters a file without a ready `SearchArtifact` for the active provider key, the provider treats the file as an unindexed candidate and verifies content through `read_content`.
     The VFS may then call `provider.index(...)` with that content and persist the returned artifact with the same version/CAS checks used on write.
     Providers compute artifacts; the VFS writes them.
   - **Batch reindex**: `vfs.reindex(namespace, provider, scope)` as an explicit command for bulk backfill.
     Runnable as a background job.

5. **Execution provider lifecycle**: Stateless per-execution initially.
   Each `execute()` call gets a fresh sandbox.
   State persistence (snapshot/restore) is a future addition via two optional protocol methods (`async def snapshot() → bytes`, `async def restore(bytes)`) that existing stateless providers simply don't implement.
   No VFS changes needed.

---

## Appendix: Prior Art & Influences

| Project / Source                                                                                                                               | Influence on ai-vfs                                                                                                                                                                                |
| ---------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [Mintlify ChromaFS](https://www.mintlify.com/blog/how-we-built-a-virtual-filesystem-for-our-assistant)                                         | Virtual filesystem over DB, invisible access pruning, grep optimization via coarse+fine filter                                                                                                     |
| [Leonie Monigatti: Virtual Filesystem over Elasticsearch](https://leoniemonigatti.com/blog/virtual-filesystem-elasticsearch.html)              | Shell commands as interface over search-backed storage; two-stage grep (ES coarse filter + regex verify); in-memory path tree; data-layer access control                                           |
| [LangChain Deep Agents: Backends](https://docs.langchain.com/oss/python/deepagents/backends#use-a-virtual-filesystem)                          | Pluggable filesystem backend protocol projecting remote storage (S3, Postgres) into the agent's tool namespace; standardized contract over native ops                                              |
| [Anthropic: Code Execution with MCP](https://www.anthropic.com/engineering/code-execution-with-mcp)                                            | Progressive disclosure, filesystem-organized tool definitions, 98.7% token reduction                                                                                                               |
| [Scaling Managed Agents: Decoupling the brain from the hands \\ Anthropic](https://www.anthropic.com/engineering/managed-agents)               | Architecture for decoupled harness, sandbox, and session persistence                                                                                                                               |
| [Cloudflare: Code Mode](https://blog.cloudflare.com/code-mode/)                                                                                | Code execution > direct tool calling, V8 isolate sandboxing                                                                                                                                        |
| [AIGNE / CSIRO: Agentic File System Abstraction](https://arxiv.org/abs/2512.05470)                                                             | Persistent context repository, memory lifecycle (history/memory/scratchpad), implemented in [AIGNE framework](https://github.com/aigne-io/aigne-framework)                                         |
| [yarnnn: Agent OS is a Filesystem](https://www.yarnnn.com/blog/the-agent-operating-system-is-a-filesystem)                                     | Three storage domains, filesystem semantics + vector acceleration backend                                                                                                                          |
| [AGFS](https://github.com/c4pt0r/agfs)                                                                                                         | Heterogeneous backends (Redis, S3, SQL, queues) under unified POSIX namespace, Plan 9 philosophy                                                                                                   |
| [Vercel just-bash](https://github.com/vercel-labs/just-bash) / [bash-tool benchmarks](https://vercel.com/blog/testing-if-bash-is-all-you-need) | TypeScript bash-like runtime with virtual filesystem; bash-tool context retrieval; tertiary JS/TS execution-provider candidate                                                                     |
| [Bashkit](https://github.com/everruns/bashkit)                                                                                                 | In-process Rust virtual bash, FileSystem trait, MountableFs, [Python bindings](https://github.com/everruns/bashkit/blob/main/crates/bashkit-python/README.md) via PyO3, JS/TS bindings via NAPI-RS |
| [Bashkit architecture docs](https://www.mintlify.com/everruns/bashkit/concepts/architecture)                                                   | Confirms modular async-first Rust core, virtual filesystem by default, and trait-based filesystem/custom builtin extension points                                                                  |
| [Pydantic Monty](https://github.com/pydantic/monty)                                                                                            | External function injection, snapshot/resume, sub-microsecond execution, explicit filesystem/network/env control                                                                                   |
| [Dirac: Hash anchors + Myers diff + single-token anchors](https://dirac.run/posts/hash-anchors-myers-diff-single-token#token-efficiency)       | Session-scoped single-token anchors plus Myers diff reconciliation for token-efficient file edits                                                                                                  |
| [Eryx](https://github.com/eryx-org/eryx)                                                                                                       | CPython 3.14 in WASM sandbox, sandbox pooling, TypedCallbacks                                                                                                                                      |
| [fsspec](https://github.com/fsspec/filesystem_spec)                                                                                            | Python ecosystem standard for filesystem abstraction                                                                                                                                               |
| [turbopuffer](https://turbopuffer.com/blog/turbopuffer) ([Latent Space interview](https://www.latent.space/p/turbopuffer))                     | S3 conditional writes for coordination-free concurrency, optimistic CAS, metadata-as-JSON-files pattern                                                                                            |
| [Fly.io Litestream VFS](https://fly.io/blog/litestream-writable-vfs/)                                                                          | Writable VFS over S3, hydration pattern, lazy content serving before full download                                                                                                                 |
| [Cursor: Fast Regex Search](https://cursor.com/blog/fast-regex-search)                                                                         | Bloom filters for regex-capable file content indexing                                                                                                                                              |
| [Dead Neurons: Forget MCP, Bash Is All You Need](https://deadneurons.substack.com/p/forget-mcp-bash-is-all-you-need)                           | MCP converging on POSIX; OS as agent runtime; composition via pipes                                                                                                                                |
| [HuggingFace smolagents](https://huggingface.co/blog/smolagents)                                                                               | Code agents > tool-calling agents; function-injection pattern                                                                                                                                      |
| [JuiceFS](https://github.com/juicedata/juicefs)                                                                                                | Metadata/data separation architecture (metadata in Redis/Postgres, data in S3)                                                                                                                     |
| [Filestash](https://github.com/mickael-kerjean/filestash)                                                                                      | 8 methods suffice to unify 20+ storage backends; MCP gateway                                                                                                                                       |
| [TigerFS](https://github.com/timescale/tigerfs)                                                                                                | Postgres-backed filesystem, ACID file writes, version history                                                                                                                                      |
| [Starlark (starlark-pyo3)](https://github.com/inducer/starlark-pyo3)                                                                           | Hermetic sandboxed execution by language design                                                                                                                                                    |
| [Yjs](https://github.com/yjs/yjs) / [ProseMirror collab](https://code.haverbeke.berlin/prosemirror/prosemirror-collab)                         | Future horizon: CRDT-based collaborative editing ([caveats](https://www.moment.dev/blog/lies-i-was-told-pt-2))                                                                                     |
