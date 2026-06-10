# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`NativeTextSearch` capability** on `SQLiteMetadataStore` (FTS5 trigram tokenizer): `index_text` upserts a content-addressed text record keyed by `(provider_key, params_hash, content_hash)` inside the version write transaction; `search_text` serves regex via FTS5 trigram prune + in-process `re` verification and fulltext via BM25 ranking — both with zero blob reads for fresh artifacts; `delete_text_artifacts` GC hook. The SQLite FTS5 availability is checked at runtime (`SQLite ≥ 3.34`); when unavailable, `native_text_search()` returns `None` and regex falls back to the guarded-reader brute-force path. Stores decoded text in the metadata DB, making it content-bearing at the same sensitivity tier as the blob store.
- **`NativeTextSearch` capability** on `PostgresMetadataStore` (tsvector + pg_trgm): `search_text` serves regex via `text ~ :pattern` evaluated in-engine against the `gin_trgm_ops` GIN index, and fulltext via `plainto_tsquery` + `ts_rank` ranking — both with zero blob reads for fresh artifacts. `initialize()` creates the `pg_trgm` extension and GIN index best-effort (logs a warning and falls back to a sequential scan if the role lacks `CREATE EXTENSION` privilege). Requires PostgreSQL with the `pg_trgm` contrib module; see migration `0002` docstring for superuser prerequisites.
- **`SearchArtifact` envelope**: frozen dataclass carrying `status` (`ready`/`failed`/`unsupported`), `schema_version`, `provider_key`, `provider_version`, `params_hash`, `content_hash`, `created_at`, `storage` (`inline`/`blob`/`external`), `error_code`, `error_message`, and either an inline `payload` or an `artifact_ref`. `is_usable()` checks status, hash currency, and — for `external` storage — external record readability/identity.
- **Guarded `ContentReader`**: per-request reader bound to the permission-pruned enumerated version set. Resolves paths to the enumerated version's `content_hash` (immune to concurrent writes), enforces `SearchLimits.max_content_reads` as a hard ceiling (raises `ReadBudgetExceededError`), and refuses out-of-scope paths. Used only for bounded straggler verification — fresh native-FTS matches never touch it.
- **`SearchRequest` / `SearchResponse`**: `SearchRequest` bundles `query`, `scope`, `search_type`, permission-pruned `search_metas`, guarded `read_content`, `SearchLimits`, and `find_predicates`. `SearchProvider.index()` now returns `SearchArtifact | None`.
- **Cold-index fail-loud with bounded straggler verification**: regex/fulltext searches over the native index classify each visible file as *fresh* (usable `SearchArtifact`) or *straggler* (missing/failed/unsupported/stale artifact). Fresh files are served with zero blob reads. A bounded set of stragglers (≤ `max_content_reads`) is verified individually via the guarded reader, lazily backfilled, and included in results. Over-budget stragglers raise `ReindexRequiredError`; index store errors raise `IndexUnavailableError` — no silent partial results.
- **`vfs.reindex(namespace, scope)`**: batch backfill via `NativeTextSearch.index_text`; binary files receive an `unsupported` artifact. Falls back to `DefaultSearchProvider.index()` when the native capability is absent.
- **Rollback `search_meta` copy**: `VFS.rollback()` copies the target version's `search_meta` to the new version; content-addressed `external` artifact references remain valid because the text record is keyed by `content_hash`, not `version_id`.
- **`FindPredicates`**: typed predicates for `FIND` searches — `name`, `size_min`/`size_max`, `mtime_after`/`mtime_before`, `type` — applied conjunctively by `DefaultSearchProvider`.
- **`get_search_meta_batch` and `update_search_artifact`** on `MetadataStore` protocol and all implemented adapters (SQLite/Postgres via the SQL base, Mongo).
- **`IndexUnavailableError`** and **`ReindexRequiredError`** error classes with actionable messages directing callers to run `vfs.reindex()`.

### Changed

- **`SearchProvider` protocol — breaking change**: `search()` now takes `SearchRequest` and returns `SearchResponse` (previously `query, scope, search_type, candidates, fetch_content → list[SearchResult]`); `index()` returns `SearchArtifact | None` (previously `dict`). The in-tree `DefaultSearchProvider` is migrated. No third-party providers exist pre-1.0.
- **Search dispatch for regex/fulltext**: when `NativeTextSearch` is present, the VFS dispatches to `search_text` (fresh path zero-blob-read, straggler path bounded); when absent, fulltext raises `SearchTypeUnsupportedError` and regex falls back to brute-force via the guarded reader.

### Deferred / Not Pursued

- **Bloom filter direction not pursued**: FTS5 trigram search answers in < 1 ms and avoids blob reads entirely on the fresh path; bloom's ~20–22% candidate pass-rate can't beat this and was superseded.

- **MongoDB accelerated regex/fulltext deferred**: MongoDB stores return `None` from `native_text_search()`; the dispatch gives them brute-force regex (guarded reader, budget-bounded) and rejects fulltext as unsupported. Whole-scope brute-force scope management is deferred.

- **PostgreSQL metadata adapter** (`PostgresMetadataStore`): full `MetadataStore` implementation on the shared SQLAlchemy Core schema using `asyncpg`, with JSONB columns for `search_meta` and `detail`. Resolves via `postgresql://` URI.

- **MongoDB metadata adapter** (`MongoMetadataStore`): full `MetadataStore` implementation using Motor, with native subdocuments for `search_meta`/`detail` and `find_one_and_update` compare-and-swap. Resolves via `mongodb://` URI; `transaction()` is best-effort (no-op) on standalone MongoDB.

- **S3 blob adapter** (`S3BlobStore`): `BlobStore` implementation using `aiobotocore`, with content-hash keys sharded under `{prefix}/{hash[0:2]}/{hash[2:4]}/{hash}`. Resolves via `s3://` URI.

- **Shared SQL schema** (SQLAlchemy Core + Alembic): single schema definition shared by SQLite and Postgres, with Alembic migration scaffolding for versioned schema evolution. `sqlalchemy` and `alembic` are now core dependencies.

- **Remote blob cache auto-enable**: when `blob_cache_enabled` is unset (auto), the `diskcache` wrapper is applied automatically for `s3://` blob stores and skipped for `file:///`.

- **`iter_versions_for_gc`** on `MetadataStore` protocol and all three adapters (SQLite/Postgres via the shared SQL base, Mongo): yields a file's non-tombstone versions in deterministic `(created_at, version_number)` order for use by the tier-window evaluator.

- **`evaluate_tier_retention`** pure function in `vfs.gc`: injectable `now` parameter, no `datetime.now()` call inside; walks version list newest-first by age band, keeps the smallest-`created_at` version per `keep_every` window, always preserves first and current versions when configured.

- **`GarbageCollector.run()` tier wiring**: `run()` now selects the tier path (`_tier_version_gc`) when `VFSConfig.retention_tiers` is explicitly configured; otherwise it falls back to the Phase 1 simple path (`_version_gc` / `list_reclaimable_versions`). `_tier_version_gc` and `evaluate_tier_retention` implement the time-window evaluator.

- **Docker Compose fixtures** for Postgres, MongoDB, and MinIO in `tests/integration/docker-compose.yaml`.

- Optional dependency extras in `pyproject.toml`: `postgres` (`asyncpg`), `mongo` (`motor`), `s3` (`aiobotocore`).

### Security

- **Canonical-path enforcement** at the VFS boundary: all public `VFS` methods now call `_require_canonical` before any permission check or storage access, rejecting paths with `..`, `.` segments, or repeated slashes (`//`). Prevents path-traversal access to resources outside the intended scope.
- **Segment-aware permission prefix matching** (`_prefix_matches`): replaces the previous raw `startswith` check so a grant on `/work` no longer covers `/workspace/file`. Matches only at segment boundaries (exact, child under `/work/`, or root `/`).
- **LIKE wildcard escaping** in SQL `list_dir`: `_like_escape` escapes `\`, `%`, and `_` in path prefixes before passing them to `LIKE … ESCAPE '\\'`, preventing `_` and `%` characters in file paths from acting as SQL wildcards.

### Fixed

- **`VersionCollisionError` + bounded write/copy/move retries**: concurrent no-CAS writes that collide on the same `version_number` now raise `VersionCollisionError` (not a raw `IntegrityError` / `DuplicateKeyError`). `VFS.write`, `copy`, and `move` retry up to 5 times, re-reading the current version number on each attempt so the next try uses N+2 when N+1 was taken by a racing writer. CAS writes (`expected_version` set) propagate `ConflictError` immediately without retrying.
- **`VFS.move()` retry stale src_file**: `src_file` is now re-read inside the retry loop so the tombstone version number is always fresh; previously all retry attempts used the same stale version number and re-collided.
- **`VFS.move()` dst double-insert on best-effort stores**: the retry loop now detects when the destination version committed on a prior attempt (standalone Mongo best-effort path) and skips re-inserting it, avoiding an extra version at the destination on tombstone-collision retries.

### Changed

- **`VFS.move()` write order**: destination version is now created _before_ the source is tombstoned. On SQL and Mongo replica-set deployments the surrounding `transaction()` keeps this atomic; on standalone MongoDB a crash between the two writes leaves the file readable at both paths (duplicate, not lost) rather than tombstoning the source before the destination exists.
- **`SQLiteMetadataStore`** re-expressed on SQLAlchemy Core (`BaseSqlMetadataStore`), sharing one schema and one CAS implementation with `PostgresMetadataStore`. Existing Phase 1 storage tests pass unchanged.
- **`check_permission` no longer applies `normpath`** to the requested path: the VFS boundary already enforces canonical paths, so in-store normalization was redundant and incorrectly stripped trailing slashes (e.g. `/team/` → `/team`), breaking exact-match checks for trailing-slash grants.
