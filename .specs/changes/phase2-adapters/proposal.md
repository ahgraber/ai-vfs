# Phase 2: Storage Adapters and Advanced Search

**Change name:** `phase2-adapters` **Date:** 2026-04-04 **Author:** ahgraber + Claude

## Intent

Extend ai-vfs beyond the local-only development profile (Phase 1) with production-grade storage adapters and accelerated search providers.
This enables deployment against managed databases and object stores, and makes grep-over-large-corpora practical via bloom filter pre-filtering.

**Prerequisite:** `phase1-core` must be synced before this change is applied.

**Design reference:** `.specs/ai-vfs-bloom-provider-design.md` — detailed bloom provider
integration design including protocol changes, scope limiting, indexing lifecycle,
error handling, and storage cost analysis.

## Scope

### In Scope

- **`PostgresMetadataStore`** (`asyncpg`): full `MetadataStore` implementation
  targeting PostgreSQL with JSONB for `search_meta` and `detail` fields
- **`MongoMetadataStore`** (`motor`): full `MetadataStore` implementation
  targeting MongoDB with native document storage for extensible fields
- **`S3BlobStore`** (`aiobotocore`): `BlobStore` implementation for S3-compatible
  object storage with content-hash keying
- **`BloomSearchProvider`**: coarse-filter/fine-filter grep acceleration —
  bloom filter indexes computed on write, candidate pre-filtering on search;
  composes `bloom-search` library (optional dependency)
- **`SemanticSearchProvider`**: embedding-based similarity search with
  vector artifacts stored in `search_meta`
- **`SearchArtifact` envelope**: replace ad-hoc per-provider keys
  (e.g., `search_meta["bloom"]`) with a standard envelope keyed by
  `provider_key`, carrying `status`, `schema_version`, `params_hash`,
  `content_hash`, and either an inline `payload` or an `artifact_ref` —
  providers own their payload schemas while the VFS handles lifecycle and
  freshness uniformly
- **SearchProvider protocol change**: `search()` accepts a `SearchRequest`
  bundling query, scope, search type, permission-pruned `search_metas`,
  a `read_content` callback, and `SearchLimits` — providers own the full
  pipeline, VFS owns blob access and permission enforcement
- **Search scope limiting**: `search_brute_force_limit` config, CWD-expanding
  heuristic for brute-force fallback paths, `SearchResponse` metadata
  (`scope_narrowed`, `actual_scope`, `total_files_in_scope`)
- **Search error degradation**: index problems degrade to brute-force, never fail
- **Coarse/fine filter pattern**: architectural pattern for search optimization —
  index narrows candidates, content verification confirms matches
- **`FindSearch` predicate expansion**: extend find from Phase 1's name-only
  matching to support size ranges, modification times, and content type via
  a typed `find_predicates` field on `SearchRequest`
- **`TierBasedRetention`**: library-side evaluator (`GarbageCollector`) for the
  time-based `RetentionPolicy.tiers` field; metadata store gains
  `iter_versions_for_gc` as a coarse enumerator so tier semantics stay
  canonical and adapters stay agnostic
- **URI resolver extensions**: register `postgresql://`, `mongodb://`, `s3://`
  schemes in the VFS store resolver

### Out of Scope

- Execution providers (Phase 3)
- fsspec compatibility bridge
- Cross-region S3 replication
- Vector database backends for semantic search (embeddings stored in `search_meta`)

## Approach

1. Implement Postgres adapter — closest to SQLite (SQL, same schema shape);
   swap `aiosqlite` for `asyncpg`, JSONB for TEXT JSON columns
2. Implement Mongo adapter — different query patterns but same protocol;
   leverage native document structure for search_meta
3. Implement S3 blob adapter — straightforward key-value mapping;
   content-hash as S3 key, prefix structure optional (S3 handles flat namespaces well)
4. Implement bloom search provider — register capabilities, `index` returns a
   `SearchArtifact` wrapping the bloom hashes; `search` consumes a
   `SearchRequest`, pre-filters candidates from `search_metas`, then calls
   `read_content` for content verification
5. Implement semantic search provider — `index` returns a `SearchArtifact`
   carrying the embedding vector (inline) or an `artifact_ref` to a vector
   store; `search` ranks candidates by cosine similarity
6. Extend URI resolver with new scheme mappings
7. Integration tests for each adapter against real services (Docker Compose test fixtures)

## Recommendations

### SQL adapter implementation: SQLAlchemy Core + Alembic (not raw asyncpg, not SQLModel)

**Recommendation:** Use `sqlalchemy.ext.asyncio` Core (query building only, no ORM) with `asyncpg` as the Postgres driver, and Alembic for schema migrations across both SQLite and Postgres.

**Rationale:**

- The project already has separate domain models in `vfs/models.py`; SQLModel would force either merging domain models with DB table definitions (coupling) or duplicating them.
  Neither is acceptable.
- Raw `asyncpg` for Postgres means two divergent implementations (SQLite via `aiosqlite`, Postgres via `asyncpg`) with separate schema DDL and row-mapping code.
  SQLAlchemy Core provides a shared schema definition and named column access (`row.field` instead of `row[3]`), reducing duplication.
- `CREATE TABLE IF NOT EXISTS` cannot handle future `ALTER TABLE` needs.
  Alembic provides versioned migration scripts with upgrade/downgrade paths — necessary once the schema evolves post-first-release.
- SQLAlchemy Core's async support (`sqlalchemy.ext.asyncio` + `asyncpg`) is mature and production-proven.

**Boundaries:**

- Use **Core only** — `Table`, `select()`, `insert()`, `Connection.execute()`.
  No ORM session, no `relationship()`, no declarative base.
- MongoDB stays a fully separate path (Motor directly).
  The `MetadataStore` protocol is the unification layer; no cross-store framework is needed or appropriate.

## Open Questions

- **S3 prefix structure**: Keep `{hash[0:2]}/{hash[2:4]}/{hash}` prefix like local FS, or use flat keys?
  S3 handles flat namespaces efficiently, but prefixes aid manual inspection via AWS console.
- **Embedding model for semantic search**: Which model/dimensions?
  Likely deferred to configuration with a sensible default.
