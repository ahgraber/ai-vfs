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
- **SearchProvider protocol change**: add `search_metas` and `read_content`
  parameters to `search()` — providers own the full pipeline, VFS owns blob access
- **Search scope limiting**: `search_brute_force_limit` config, CWD-expanding
  heuristic for brute-force fallback paths, `SearchResponse` metadata
  (`scope_narrowed`, `actual_scope`, `total_files_in_scope`)
- **Search error degradation**: index problems degrade to brute-force, never fail
- **Coarse/fine filter pattern**: architectural pattern for search optimization —
  index narrows candidates, content verification confirms matches
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
4. Implement bloom search provider — register capabilities, index method returns
   bloom hashes, search method pre-filters candidates before content verification
5. Implement semantic search provider — index method computes embeddings,
   search method ranks by cosine similarity
6. Extend URI resolver with new scheme mappings
7. Integration tests for each adapter against real services (Docker Compose test fixtures)

## Open Questions

- **Postgres connection pooling**: Use `asyncpg.Pool` directly or wrap with `sqlalchemy.ext.asyncio`?
  Leaning toward raw `asyncpg` for simplicity.
- **S3 prefix structure**: Keep `{hash[0:2]}/{hash[2:4]}/{hash}` prefix like local FS, or use flat keys?
  S3 handles flat namespaces efficiently, but prefixes aid manual inspection via AWS console.
- **Embedding model for semantic search**: Which model/dimensions?
  Likely deferred to configuration with a sensible default.
