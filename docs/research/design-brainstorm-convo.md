# ai-vfs Design Brainstorming Conversation

_Exported 2026-04-04_

---

## Context

Brainstorming session to design a virtual filesystem library for AI agents.
Started from extensive prior research (memo, sandbox research, prior art survey) and iterated through clarifying questions, approach selection, and section-by-section design review.

---

## Project Context & Research

Read all materials in `research/`:

- `vfs-memo.md` — synthesized the convergence on virtual filesystems as agent infrastructure
- `vfs-landscape-and-fit.md` — consolidated prior-art, sandbox, integration, and backend-fit research (the multi-turn in-process sandbox research, the 34-tool sandbox survey, the fsspec/TigerFS integration patterns across the sandboxes, the 10-project landscape survey, and the storage/search + ZeroFS/FUSE fit analyses)
- `tigerfs-sandbox-demos.md` — proof-of-concept for bashkit + TigerFS and Monty + TigerFS composition
- `pages/*.md` — summaries of all inspiration sources (Mintlify, Anthropic, Cloudflare, Vercel, AIGNE, yarnnn, AGFS, etc.)

Also researched turbopuffer's S3-based concurrency model (conditional writes, CAS on metadata files).

---

## User Preferences (stated upfront)

- S3-compatible blob for file storage, pluggable DB for metadata (SQL and NoSQL adapters)
- Sandboxed, in-process bash/python supporting file read/write/edit
- File access permissions and versioning/rollback
- Self-hostable

---

## Q1: Deployment Model

**Question:** Library (A), Service (B), or Both (C)?

**Answer:** (A) for personal use, but might license to work → effectively (C).
Library-first, service wrapper as future concern.

**Decision:** Library-first SDK.
Core is embeddable; server wrapper is a deployment convenience for later.

---

## Q2: Execution Scope

**Question:** Orchestration-only (A), general-purpose (B), or pluggable tiers (C)?

**Answer:** (C) — pluggable execution with simple default (A), leave room to expand.

**Decision:** Define an `ExecutionProvider` protocol.
Ship with Bashkit + Monty as initial tier.
Users can plug in Eryx, E2B, etc.

---

## Q3: Versioning Model

**Question:** Snapshot-based (A), per-file version history (B), or git-like branching (C)?

**Answer:** (B) — each file needs "undo."
Future horizon: CRDT-based collaborative editing (Yjs, ProseMirror collab), but far out.

**Decision:** Per-file immutable versions.
Each write creates a new version.
Undo = set current pointer to previous version.
Content-addressed blobs mean rollback is a metadata operation (no blob copy).

---

## Q4: Permissions Model

**Question:** Path-prefix isolation (A), RBAC (B), or capability-based (C)?

**Answer:** (A) with possible expansion into (B).
Lives in the metadata layer.

**Decision:** Path-prefix ownership initially.
Metadata DB is the authority on permissions.
Schema supports future per-path RBAC without migration.

---

## Q5: VFS Scope

**Question:** File storage only (A) or unified namespace for tools/memory/context (B)?

**Answer:** (A).
The agent framework builds the unified namespace on top.

**Decision:** VFS is infrastructure — stores files and directories.
The "everything is a file" agent namespace is a consumer-layer concern.

---

## Q6: Metadata DB Adapter

**Question:** Abstract repository interface (A) or SQL-first with ORM (B)?

**Answer:** (A).

**Decision:** `MetadataStore` protocol with ~10 methods.
SQLite first, then Postgres, then Mongo/Cosmos.
No ORM dependency.

---

## Approach Selection

Three approaches proposed:

- **A: fsspec-native** — build on fsspec directly.
  Pro: ecosystem compat.
  Con: no built-in versioning/permissions, awkward composition with metadata sidecar.
- **B: Custom VFS with fsspec bridge** — purpose-built VFS with own protocol, fsspec adapter on top.
  Pro: clean domain model, versions/permissions first-class.
  Con: more upfront design.
- **C: Bashkit native VFS trait** — Rust `FileSystem` trait with PyO3 bindings.
  Pro: tight sandbox integration.
  Con: requires Rust for core.

**Selected: Approach B.** Clean Python-native domain model. fsspec and bashkit traits become adapters, not foundations.

---

## "What Are You Missing?" — Identified Gaps

1. **Content-addressing / deduplication** — BLAKE3 hash as blob key.
   Identical content across agents shares one blob.
2. **Garbage collection** — Time Machine retention: keep N recent + decay over time.
   Blob GC deletes unreferenced content hashes.
3. **Event/audit log** — Append-only log of all state changes.
   Same metadata DB.
   Carries OTel trace IDs.
4. **Lazy content resolution** — `list` and `stat` are metadata-only.
   `read` fetches blobs.
   Never eager.
5. **Search** — Path glob + content grep built-in.
   Bloom filter acceleration and semantic search as plugins.
   User's own bloom-search as potential integration.
6. **Concurrency** — Optimistic concurrency via version stamps (CAS).
   Immutable blobs = no conflicts.
   Borrowed from turbopuffer's S3 conditional writes pattern.

---

## Hashing Decision

- **BLAKE3** for content-addressing (cryptographic, fast, collision-resistant)
- **xxhash** for internal indexing structures (bloom filters, hash tables)
- **Rensa** for future near-duplicate detection, not content addressing

---

## Turbopuffer Concurrency Model

Researched turbopuffer's architecture:

- S3 strong read-after-write consistency (since Dec 2020)
- Conditional writes (CAS) for metadata coordination (since late 2024)
- Metadata as JSON files updated via CAS — no external coordination layer

**Decision:** Don't replace the metadata DB.
Borrow the concurrency model:

- Metadata DB uses optimistic concurrency via version stamps (`UPDATE WHERE version = expected`)
- Blob store is append-only and immutable (content-addressed = idempotent PUTs)
- Dropped the S3-only minimal profile per user preference

---

## Design Sections (all approved)

### Section 1: Core Domain Model

Four concepts: Namespace, File, Version, Principal.

Operations: `stat`, `read`, `write`, `delete`, `list`, `search`, `versions`, `rollback`.

Key decisions:

- Lazy content (list/stat never touch blob store)
- Delete is a tombstone version
- Rollback creates a new version pointing to old content hash
- Optimistic concurrency via optional `expected_version`

### Section 2: Layer Architecture

Four layers:

1. **Consumer Layer** — agent frameworks, CLI, fsspec bridge
2. **VFS Layer** — orchestrator (permissions, versioning, concurrency, retention, OTel)
3. **Protocol Layer** — four pluggable protocols:
   - MetadataStore (~10 methods)
   - BlobStore (3 methods: put, get, delete)
   - SearchProvider (3 methods: index, search, capabilities)
   - ExecutionProvider (3 methods: execute, capabilities, reset)
4. **Adapter Layer** — concrete implementations (SQLite, Postgres, Mongo, S3, MinIO, bloom, Monty, Bashkit, etc.)

Plus a **Shell Operations Layer** between execution and VFS — wrappers that translate bash command signatures (`grep`, `find`, `glob`, `cat`, `ls`, `head`, `tail`) into VFS operations.
Enables Mintlify-style optimization (search index as coarse filter, content verify as fine filter).

### Section 3: Versioning & Retention

Per-file immutable versions with ULID as internal ID and monotonic `version_number` as human-facing counter.

Time Machine retention:

- Last N versions (default 50)
- Last 24h: all versions
- Last 7d: one per hour
- Last 30d: one per day
- Beyond 30d: one per week
- Hard floor: always keep current + initial version

GC: background process — version GC applies retention, blob GC deletes unreferenced content hashes.

### Section 4: Permissions Model

Path-prefix ownership with default-deny.
Invisible pruning (unauthorized paths don't appear in listings).

Permission record: `(principal_id, namespace_id, path_prefix, operations)`.

All IDs are ULIDs with a names table for human-friendly display names.

Future expansion: per-path RBAC with roles mapping to operation sets.
No schema change needed.

### Section 5: Observability & Audit

OTel spans on all VFS operations with attributes (namespace, path, principal, version, content_hash, blob_size).
Child spans for sub-operations.
Metrics for latency, throughput, cache hits.

Audit log: append-only in metadata DB.
Records writes, deletes, rollbacks, permission changes, GC runs.
Carries OTel trace ID for correlation.
Reads are OTel-only (too noisy for audit).

### Section 6: Search Architecture

Built-in: path glob, find (metadata predicates), content grep.

Pluggable via SearchProvider protocol:

- Bloom filter provider: indexes content on write, tests bloom filters on search, verifies candidates against content
- Semantic provider: computes embeddings on write, vector similarity on search

Search metadata stored as extensible `search_meta` dict per version in metadata DB.

Dispatch: VFS routes search to provider declaring matching capability, falls back to brute-force read-and-match.

### Section 7: Execution Model

ExecutionProvider protocol: `execute(code, fs_ops, timeout, resource_limits)`.

FsOperations: injected VFS callbacks (read, write, list, stat, delete) + shell wrappers (grep, find, glob, head, tail).

VFS constructs permission-scoped callbacks bound to principal and namespace.

Initial providers: Bashkit (bash), Monty (Python subset).
Future: Eryx, PyMiniRacer, E2B.

Resource limits: timeout, max memory, max VFS operations per execution.

### Section 8: Configuration

pydantic-settings for config.
URI-based store resolution.
Sensible local defaults (SQLite + local FS).

Three deployment profiles: Local dev, Self-hosted, Production. (Dropped minimal/S3-only profile.)

---

## Final Summary

**ai-vfs: A virtual filesystem library for AI agents**

- Core model: Namespaces, Files, Versions, Principals — ULIDs with human-friendly names
- Architecture: VFS orchestration over four pluggable protocols (MetadataStore, BlobStore, SearchProvider, ExecutionProvider)
- Storage: Content-addressed blobs (BLAKE3) in S3-compatible storage; metadata in pluggable DB (SQLite/Postgres/Mongo)
- Versioning: Per-file immutable versions, Time Machine retention, rollback via new version pointing to old content hash
- Permissions: Path-prefix ownership, default-deny, invisible pruning, future RBAC expansion
- Concurrency: Optimistic via version stamps (CAS), immutable blobs
- Search: Built-in glob/find/grep, pluggable bloom acceleration and semantic search
- Execution: Pluggable sandboxes (Bashkit, Monty initially), VFS ops injected as callbacks, shell ops translation layer
- Observability: OTel spans + audit log with trace ID correlation
- Config: pydantic-settings, local defaults, URI-based store resolution
