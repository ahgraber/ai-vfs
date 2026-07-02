# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Code-mode execution mount**: `vfs.execute` runs sandboxed code over the governed VFS through a native filesystem mount. In Monty, the sandbox's `open`/`pathlib`/`os` calls route to the VFS via an `AbstractOS` adapter over an internal async **FS-port** boundary; code-mode editing is plain native file I/O (`open(path, "w").write(...)` / `pathlib`). Permissions, audit, and CAS hold on every mounted operation, and the host filesystem is never exposed.
- **just-bash execution provider**: run Bash over the governed VFS — builtins (`cat`, `ls`, redirection, pipes) operate on VFS files through the FS-port, and `grep`/`find`/`glob` are overridden to resolve via the VFS search index (parity with Monty). Behind the `just-bash` extra.
- **Execution sandboxes as optional extras**: `monty` and `just-bash` are optional extras with a `codemode` umbrella (`ai-vfs[monty,just-bash]`); the core VFS installs and runs without either, resolving providers lazily with an actionable install hint when an extra is missing.
- **`NativeTextSearch` capability** on `SQLiteMetadataStore` (FTS5 trigram tokenizer): `index_text` upserts a content-addressed text record keyed by `(provider_key, params_hash, content_hash)` inside the version write transaction; `search_text` serves regex via FTS5 trigram prune + in-process `re` verification and fulltext via a dedicated non-stemming `unicode61` word-token FTS5 index (BM25 ranking) — both with zero blob reads for fresh artifacts; `delete_text_artifacts` GC hook. The SQLite FTS5 availability is checked at runtime (`SQLite ≥ 3.34`); when unavailable, `native_text_search()` returns `None` and regex falls back to the guarded-reader brute-force path. Stores decoded text in the metadata DB, making it content-bearing at the same sensitivity tier as the blob store.
- **`NativeTextSearch` capability** on `PostgresMetadataStore` (tsvector + pg_trgm): `search_text` serves regex via `text ~ :pattern` evaluated in-engine against the `gin_trgm_ops` GIN index, and fulltext via the non-stemming `'simple'` text-search config (`plainto_tsquery('simple', …)` + `ts_rank`) — both with zero blob reads for fresh artifacts. `initialize()` creates the `pg_trgm` extension and GIN index best-effort (logs a warning and falls back to a sequential scan if the role lacks `CREATE EXTENSION` privilege). Requires PostgreSQL with the `pg_trgm` contrib module; see migration `0002` docstring for superuser prerequisites.
- **`SearchArtifact` envelope**: frozen dataclass carrying `status` (`ready`/`failed`/`unsupported`), `schema_version`, `provider_key`, `provider_version`, `params_hash`, `content_hash`, `created_at`, `storage` (`inline`/`blob`/`external`), `error_code`, `error_message`, and either an inline `payload` or an `artifact_ref`. `is_usable()` checks status and hash currency (`content_hash` + `params_hash`).
- **Guarded `ContentReader`**: per-request reader bound to the permission-pruned enumerated version set. Resolves paths to the enumerated version's `content_hash` (immune to concurrent writes), enforces `SearchLimits.max_content_reads` as a hard ceiling (raises `ReadBudgetExceededError`), and refuses out-of-scope paths. Used only by the brute-force fallback path (regex when no native capability is present); native-FTS matches never touch it.
- **`SearchRequest` / `SearchResponse`**: `SearchRequest` bundles `query`, `scope`, `search_type`, permission-pruned `search_metas`, guarded `read_content`, `SearchLimits`, `find_predicates`, and `match_mode` (FULLTEXT-only). `SearchProvider.index()` now returns `SearchArtifact | None`.
- **Cold-index fail-loud**: regex/fulltext over the native index classify each visible file as *decided* (identity-current artifact — a `ready` artifact answers; an `unsupported`/`failed` artifact is a confirmed non-match) or *straggler* (missing or identity-drifted artifact). Decided files are served with zero blob reads; any straggler raises `ReindexRequiredError` naming a path-scoped `reindex` (no query-time verification, backfill, or approximation), and index-store errors raise `IndexUnavailableError`.
- **`vfs.reindex(namespace, scope)`**: batch backfill via `NativeTextSearch.index_text`; binary files receive an `unsupported` artifact. Falls back to `DefaultSearchProvider.index()` when the native capability is absent.
- **Derived-version `search_meta` propagation**: `VFS.rollback()`, `copy()`, and `move()` copy the source version's `search_meta` to the new version; content-addressed `external` artifact references stay valid because the text record is keyed by `content_hash`, not `version_id` — so a copied, moved, or rolled-back file is searchable immediately with no reindex.
- **`FindPredicates`**: typed predicates for `FIND` searches — `name`, `size_min`/`size_max`, `mtime_after`/`mtime_before`, `type` — applied conjunctively by `DefaultSearchProvider`.
- **`get_search_meta_batch` and `update_search_artifact`** on `MetadataStore` protocol and all implemented adapters (SQLite/Postgres via the SQL base, Mongo).
- **`IndexUnavailableError`** and **`ReindexRequiredError`** error classes with actionable messages directing callers to run `vfs.reindex()`.
- **`FullTextMatchMode` (`ALL`/`ANY`)**: new enum on `SearchRequest`; `vfs.search` and `session.search` gain a `match_mode` keyword (default `ALL`, backward-compatible). `ANY` returns the ranked-OR union (a document matching at least one term), ranked by descending relevance (FTS5 BM25 / Postgres `ts_rank`). Applies only to FULLTEXT; ignored for GLOB/FIND/REGEX.
- **Word-tokenized FULLTEXT representation**, distinct from the trigram representation used for REGEX: SQLite `unicode61` FTS5 table, Postgres `'simple'` config — both non-stemming and language-neutral, with no minimum token length so short terms like `s3` are matchable. Built once from the stored `raw_text` at store init via an idempotent, crash-resumable anti-join (no blob reads); `params_hash` is unchanged.
- **FULLTEXT query term cap (128)** enforced at the `vfs.search` boundary, bounding `ANY`-mode per-term query growth.
- **`vfs.execute` observability**: each invocation opens a `vfs.execute` OTel span that parents the inner file-operation spans, records an operation-count/duration metric, and emits a single invocation-level audit event (`operation="execute"`, `path=cwd`, `detail` = provider + outcome + `error_type` on failure) — distinct from and in addition to the per-operation audit events of any files the code mutates. Tier-1 rejections (denied permission, unknown provider, non-canonical cwd) are not audited (no code ran).
- **Resource-limit surface for sandboxed execution**: `ResourceLimits.max_write_bytes` caps a single write; `ExecutionCapabilities.enforces_memory_limit` lets callers feature-detect which provider honours `max_memory_bytes`; `ResourceLimitExceededError` is raised by the FS-port on an oversized native read/write (mapped to `error_type="budget_exceeded"`).
- **`google-re2`** core dependency: content regex search now uses the linear-time RE2 engine (see Security).

### Changed

- **`SearchProvider` protocol — breaking change**: `search()` now takes `SearchRequest` and returns `SearchResponse` (previously `query, scope, search_type, candidates, fetch_content → list[SearchResult]`); `index()` returns `SearchArtifact | None` (previously `dict`). The in-tree `DefaultSearchProvider` is migrated. No third-party providers exist pre-1.0.
- **Search dispatch for regex/fulltext**: when `NativeTextSearch` is present, the VFS dispatches to `search_text` (fresh path zero-blob-read; any straggler fails loud with `ReindexRequiredError`); when absent, fulltext raises `SearchTypeUnsupportedError` and regex falls back to brute-force via the guarded reader.
- **FULLTEXT now matches whole word tokens, not trigram substrings or English stems** (behavior change): `cat` no longer matches `category`, `databases` no longer matches `database` on Postgres, and short terms like `s3` now match correctly. REGEX still matches substrings via the unchanged trigram representation. On upgrade, the SQLite word index is rebuilt from stored text automatically (no blob reads); Postgres needs no migration (its tsvector is computed inline). SemVer: MINOR (additive API; the result-set change is a deliberate, documented correctness fix, permitted pre-1.0).
- **Blob GC reference-check and text-artifact deletion are now atomic** (one metadata transaction): a `content_hash` with a live version reference is never swept. The subsequent blob delete is best-effort; the cross-store revive race is an accepted PoC limitation.
- **FS-port native mount now enforces `ResourceLimits`**: the operation budget and `max_read_bytes`/`max_write_bytes` caps are enforced by a single counter shared between the injected `FsOperations` verbs and the `SessionFsPort` native mount, so sandboxed `open`/`pathlib` file I/O — the primary interaction surface — is governed identically to the injected verbs (previously the mount was ungoverned).
- **just-bash provider reflects the script's exit code**: a script that runs to completion but exits non-zero now returns `ExecutionResult(success=False, error_type="nonzero_exit")` with its stderr in `error_message` (previously every run reported `success=True` and discarded `exit_code`/`stderr`).
- **Content REGEX semantics (RE2)**: patterns are matched line-by-line with RE2, so backreferences and lookaround are unsupported (an unusable pattern yields no matches rather than raising); REGEX results are now identical across SQLite/Postgres/in-memory backends (Postgres no longer applies an anchor-sensitive whole-document `~` prune that could differ from per-line matching).

### Removed

- **Query-time search self-healing**: native search no longer verifies stragglers via blob reads, lazily backfills the index, re-checks external-record existence, or approximates FULLTEXT in-process. A fresh index is authoritative; a stale one fails loud (`ReindexRequiredError`) and `vfs.reindex(scope=…)` is the remedy. The guarded reader and `max_content_reads` budget survive only on the brute-force fallback path (REGEX with no native capability).
- **`SearchArtifact.is_usable()` external-record parameters** (`external_readable` / `external_identity_match`) and the `has_text_artifacts` store method — the text record is content-addressed and resident in the metadata store, so an identity-current artifact's record is always present.

### Security

- **ReDoS in content regex closed**: agent-supplied `grep` patterns previously ran on Python's backtracking `re` engine synchronously on the host event loop, where a catastrophic pattern (e.g. `(a+)+$`) could hang the process past the execution timeout (`asyncio.wait_for` cannot interrupt synchronous CPU work). All in-process regex verification (SQLite/Postgres/in-memory) now uses the linear-time RE2 engine, so no pattern can be super-linear.
- **Sandbox host-OOM / budget-bypass closed**: the native filesystem mount (`open`/`pathlib`/bash redirection) no longer bypasses `max_read_bytes`/`max_write_bytes` or the operation budget — a large native read/write is refused before the blob reaches host memory, for both the Monty and just-bash providers.
- **`vfs.execute` now attributable**: an invocation running arbitrary code that mutates files emits its own audit event and span, closing the accountability gap where only the inner writes were recorded.

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
