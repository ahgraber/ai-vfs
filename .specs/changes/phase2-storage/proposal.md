# Phase 2 (Storage): Production Metadata and Blob Adapters

**Change name:** `phase2-storage` **Date:** 2026-05-22 **Author:** ahgraber + Claude

## Intent

Extend ai-vfs beyond the Phase 1 local-only profile (SQLite + local FS) with production-grade storage adapters: PostgreSQL and MongoDB metadata stores and an S3-compatible blob store.
This lets ai-vfs deploy against managed databases and object stores while keeping the `MetadataStore`/`BlobStore` ports unchanged for consumers.

This change is the **storage half** of the former `phase2-adapters` change, split out so the production backends can land independently of the search redesign.
The **search half** — the `SearchArtifact` envelope, the `SearchRequest`/`SearchResponse` protocol, the guarded content reader, and the full-text search providers — lives in `phase2-search` and does not block this change.
Storage adapters persist `search_meta` as an opaque manifest field; they do not need to understand its shape.

**Prerequisite:** `phase1-core` and `shell-context` must be synced (they are archived).

## Scope

> Listed in build-dependency order; `design.md` Decisions and `tasks.md` groups follow
> the same order.

### In Scope

- **URI resolver extensions** (foundation): register `postgresql://`, `mongodb://`,
  `s3://` schemes so the new adapters are constructible at VFS construction time.
- **SQL schema tooling**: SQLAlchemy Core (query building only — no ORM) for a single shared SQLite/Postgres schema definition, plus Alembic migration scaffolding for versioned schema evolution.
  The Phase 1 `SQLiteMetadataStore` is re-expressed on Core so both SQL backends share one schema and CAS implementation.
- **`PostgresMetadataStore`** (`asyncpg`): full `MetadataStore` implementation targeting
  PostgreSQL with JSONB for the `search_meta` and `detail` fields.
- **`MongoMetadataStore`** (`motor`): full `MetadataStore` implementation targeting
  MongoDB with native document storage for extensible fields; CAS via
  `find_one_and_update` with version match.
- **`S3BlobStore`** (`aiobotocore`): `BlobStore` implementation for S3-compatible object
  storage, content-hash keyed under the same `{hash[0:2]}/{hash[2:4]}/{hash}` sharded
  layout as the local FS adapter.
- **Remote blob cache auto-enable**: when `blob_cache_enabled` is unset (auto), wrap
  remote blob stores (`s3://`) in the `diskcache` layer; leave `file:///` unwrapped.
- **`move()` partial-failure safety**: order `move()` writes destination-before-source so a
  crash between the two metadata writes leaves the move _partially applied_ (no version
  lost — append-only history retains prior content), never a lost file — the safety net
  when `MetadataStore.transaction()` is not fully atomic (standalone Mongo).
- **`TierBasedRetention`**: library-side evaluator (`GarbageCollector`) for the
  time-based `RetentionPolicy.tiers` field; the metadata store gains
  `iter_versions_for_gc` as a coarse enumerator so tier semantics stay canonical in the
  library and adapters stay agnostic.

### Out of Scope

- **All search** — the `SearchArtifact` envelope, the `SearchRequest`/`SearchResponse` protocol, the guarded reader, the `NativeTextSearch` capability _and its SQLite/Postgres implementations_, the `search_text_artifacts` table, search-artifact GC, dispatch, and degradation all belong to `phase2-search`.
  This change builds only the metadata/blob adapters; `phase2-search` adds the FTS machinery onto the store classes it creates.
- Execution providers (Phase 3).
- fsspec compatibility bridge.
- Cross-region S3 replication.
- Multi-document Mongo transactions beyond what a replica set provides (standalone Mongo
  relies on the `move()` ordering safety net above).

## Approach

Build in dependency order — foundation, then adapters, then retention:

1. Extend the URI resolver with `postgresql://`, `mongodb://`, `s3://` mappings.
2. Re-express the schema on SQLAlchemy Core with Alembic; port `SQLiteMetadataStore` to
   it first and prove the Phase 1 storage tests pass unchanged.
3. Implement `PostgresMetadataStore` on the shared Core schema (`asyncpg`, JSONB).
4. Implement `MongoMetadataStore` (Motor; `find_one_and_update` CAS; `transaction()` a
   documented best-effort no-op on standalone Mongo).
5. Reorder `VFS.move()` to write destination-before-source for non-destructive partial
   failure.
6. Implement `S3BlobStore` (`aiobotocore`; sharded content-hash keys) and auto-enable the
   blob cache for `s3://`.
7. Add `TierBasedRetention` — `iter_versions_for_gc` enumerator on the protocol plus the
   client-side tier-window evaluator in `GarbageCollector`.
8. Integration tests for each adapter against real services (Docker Compose fixtures),
   including a cross-adapter tier-reclamation equivalence test.

## Open Questions

None. (S3 prefix structure and SQL framework choice are resolved in `design.md`.)
