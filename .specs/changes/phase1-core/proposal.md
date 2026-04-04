# Phase 1: Core Library

**Change name:** `phase1-core` **Date:** 2026-04-04 **Author:** ahgraber + Claude

## Intent

Implement the foundational ai-vfs library — domain models, protocols, storage adapters,
search provider, observability, and VFS orchestrator — delivering a working local
development deployment profile: SQLite metadata + local FS blobs + default search.

This is the baseline implementation against which all 7 capability specs (file-operations, versioning, access-control, storage, observability, search, storage) were derived.
No spec changes are required — this change purely implements the existing baseline specs, minus execution providers (Phase 3).

## Scope

### In Scope

- **Domain models**: `FileMeta`, `VersionMeta`, `Permission`, `AuditEvent`,
  `RetentionPolicy`, `Principal`, `Namespace`, `Name`, `SearchResult`
- **Custom exceptions**: `ConflictError`, `PermissionDeniedError`, `NotFoundError`
- **Protocol definitions**: `MetadataStore`, `BlobStore`, `SearchProvider`
- **Configuration**: `VFSConfig` (pydantic-settings, `AIFS_` env prefix, local defaults)
- **`LocalFSBlobStore`**: content-addressed storage with `{hash[0:2]}/{hash[2:4]}/{hash}`
  prefix directory structure; idempotent puts
- **`CachedBlobStore`**: diskcache-backed LRU wrapper; enabled by default for remote stores
- **`SQLiteMetadataStore`**: full `MetadataStore` implementation including CAS semantics
  (`WHERE version_number = ?`), permission queries (most-specific-prefix sort),
  audit events, search_meta, names table, GC queries
- **`DefaultSearchProvider`**: glob, find, regex/grep (brute-force: list + read + match)
- **OTel helpers**: span creation with VFS attributes, child spans, metrics;
  all no-ops when `otel_enabled=False`
- **Audit log**: append-only `AuditEvent` persistence with OTel `trace_id` correlation
- **`VFS` orchestrator**: `read`, `write`, `delete`, `stat`, `list`, `versions`,
  `rollback`, `search`; permission enforcement and invisible pruning on all operations
- **`GarbageCollector`**: version GC (retention policy) + blob GC (orphan detection)
- **Public API**: `VFS` class with URI-based store resolution at construction,
  `initialize()` / `close()` lifecycle
- **Process identification**: `setproctitle("ai-vfs: <role>")` for service/GC processes

### Out of Scope (Phase 2+)

- Postgres metadata adapter (`asyncpg`)
- MongoDB metadata adapter (`motor`)
- S3 blob adapter (`aiobotocore`)
- Bloom filter search provider
- Semantic search provider
- Execution providers (Bashkit, Monty) and shell operations layer
- fsspec compatibility bridge

## Approach

Bottom-up, dependency-ordered:

01. Domain models + exceptions
02. Protocol definitions (MetadataStore, BlobStore, SearchProvider)
03. Configuration (VFSConfig)
04. `LocalFSBlobStore` + `CachedBlobStore`
05. `SQLiteMetadataStore`
06. `DefaultSearchProvider`
07. OTel helpers + audit log
08. `VFS` orchestrator (per-operation, built on top of the layer below)
09. `GarbageCollector`
10. Public API (`__init__.py`, URI resolution, lifecycle)

Each component is implemented test-first.
Integration tests exercise the full stack (VFS → SQLite + LocalFS) to verify cross-layer behavior.

## Open Questions

None — design is fully resolved in `.specs/ai-vfs-design-doc.md`.
