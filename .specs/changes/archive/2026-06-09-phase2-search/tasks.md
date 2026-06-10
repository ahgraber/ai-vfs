# Tasks: Phase 2 (Search)

## Envelope & Protocol Foundation

- [x] Define the `SearchArtifact` frozen dataclass (status/schema_version/provider_key/provider_version/params_hash/content_hash/created_at/storage/payload/artifact_ref/error_code/error_message) and a usability helper (`ready` + content_hash + params_hash match; for `external`, referenced record readable + identity-matched)
- [x] Define `SearchRequest`, `SearchResponse`, `SearchLimits` (`max_content_reads`), `SearchMetaEntry`, and `find_predicates` types
- [x] Migrate the `SearchProvider` protocol: `search(SearchRequest) -> SearchResponse`, `index(...) -> SearchArtifact | None`
- [x] Migrate `DefaultSearchProvider` to the new signatures (glob/find from `search_metas`, metadata-only; `index` returns `None`)
- [x] Add `get_search_meta_batch(version_ids)` and `update_search_artifact(version_id, provider_key, artifact)` to the `MetadataStore` protocol and **all implemented metadata stores** (SQLite here; Postgres/Mongo, built in `phase2-storage`, must implement the same protocol additions — coordinate the cross-change dependency)
- [x] Test: artifact usability — ready+matching usable; content_hash drift, params_hash drift, missing/mismatched external record → straggler (`SearchArtifactEnvelope`/`ReadyArtifactUsable`, `ContentHashMismatchIsStale`, `ParamsHashMismatchIsStale`, `ExternalRecordMissingOrMismatchedIsStale`)
- [x] Test: a `SearchArtifact` round-trips all envelope fields through `search_meta` (incl. `provider_version`, `created_at`, error fields)
- [x] Test: default provider under `SearchRequest` serves glob/find from metadata and returns a `SearchResponse`; `index` returns `None` (`SearchProviderProtocol`/`DefaultProviderMigratedToRequest`, `IndexReturnsArtifactOrNone`)
- [x] Test: `search_meta` round-trips an `external` artifact referencing a content-addressed text record (`SearchMetadataExtensible`/`ManifestReferencesExternalTextRecord`)

## Guarded Content Reader

- [x] Implement the guarded reader: resolve path → enumerated entry's `content_hash` (fetch blob by hash), enforce `max_content_reads` (raise `ReadBudgetExceeded`), refuse out-of-scope paths; used only on the straggler path
- [x] Wire the VFS search path to build the guarded reader from the permission-pruned entry set
- [x] Test: reader returns the enumerated version's content after a concurrent newer write (`GuardedContentReader`/`ReadsEnumeratedVersionNotLatest`)
- [x] Test: the (N+1)th read raises `ReadBudgetExceeded` (`GuardedContentReader`/`BudgetCeilingEnforced`)
- [x] Test: a path absent from `search_metas` is refused (`GuardedContentReader`/`OutOfScopePathRefused`)

## NativeTextSearch Capability — Protocol & Dispatch

- [x] Define the `NativeTextSearch` capability protocol (`index_text`, `search_text(request, visible_version_ids)`, `delete_text_artifacts(content_hashes, retired_params_hashes)`) and the `native_text_search()` accessor on `MetadataStore`
- [x] Specify the content-addressed text identity `(provider_key, params_hash, content_hash)` and the content→visible-occurrence expansion contract (result identity from the VFS-enumerated version)
- [x] Wire VFS search dispatch: regex/fulltext → `native_text_search().search_text(...)` when present; else unsupported on that backend (Mongo); glob/find always via `DefaultSearchProvider`
- [x] Test: regex routes to the capability; glob/find always served from metadata; Mongo regex/fulltext rejected as unsupported (`PluggableSearchProviders`/`NativeCapabilityServesRegex`, `GlobFindAlwaysAvailable`, `MongoRegexDeferred`)

## NativeTextSearch Implementation (on the phase2-storage store classes)

> Depends on `phase2-storage` having built the Core schema and the SQLite/Postgres store classes.

- [x] Add the content-addressed `search_text_artifacts` table keyed by `(provider_key, params_hash, content_hash)` to the `phase2-storage` Core schema + an Alembic migration; store the raw decoded text plus the derived index (SQLite FTS5 external-content; Postgres `tsvector` + `pg_trgm` GIN)
- [x] Implement `NativeTextSearch` on `SQLiteMetadataStore`: `index_text` (in the version txn; strict-decode → `unsupported` on failure), `search_text` (FTS5 trigram prune + in-process regex verify over stored text; `bm25` for fulltext; join to visible versions for occurrence identity), `delete_text_artifacts`, `native_text_search()` accessor
- [x] Implement `NativeTextSearch` on `PostgresMetadataStore`: same contract via `text ~ :pattern` over `pg_trgm` GIN (in-engine regex) and `ts_rank` for fulltext
- [x] Implement `native_text_search()` returning `None` on `MongoMetadataStore`
- [x] Fold text-artifact GC into the `GarbageCollector` blob-orphan sweep (delete text artifacts when `content_hash` is orphaned) and add a retired-`params_hash` sweep — derived, no eager ref-count
- [x] Test: shared content across two versions yields one text row referenced by both (`NativeTextSearchStorage`/`ContentAddressedTextDedup`)
- [x] Test: text artifact commits in the version transaction; neither persists on rollback (`NativeTextSearchStorage`/`IndexTextInVersionTransaction`)
- [x] Test: orphaned `content_hash` deletes blob and text artifacts together (`NativeTextSearchStorage`/`TextArtifactGcFollowsContentOrphan`)
- [x] Test: `MongoMetadataStore.native_text_search()` returns `None` (`NativeTextSearchStorage`/`MongoHasNoNativeTextSearch`)
- [x] Test (unit, SQLite): write produces a content-addressed text record + `ready` `external` artifact in the write txn (`NativeTextSearchCapability`/`IndexOnWriteProducesExternalArtifact`)
- [x] Test (unit, SQLite): accelerated regex returns matches with zero guarded-reader blob reads for fresh records (`NativeTextSearchCapability`/`AcceleratedRegexAvoidsBlobReads`)
- [x] Test (unit, SQLite): fulltext returns relevance-ranked results (`NativeTextSearchCapability`/`RankedFulltext`)
- [x] Test (unit, SQLite): identical content at two paths → both returned from one content match (`NativeTextSearchCapability`/`ContentMatchExpandsToVisibleOccurrences`)
- [x] Test (unit, SQLite): a rolled-back version reports its own path/version for a content match, not the source version's (`NativeTextSearchCapability`/`IdentityFromVisibleVersionAfterRollback`)
- [x] Test (contract, SQLite unit + Postgres integration): identical matching-path set across both implementations and the brute-force baseline; query set MUST include a trigram-unfriendly pattern (e.g. `[0-9]+`) to exercise the sequential-scan fallback path (`NativeTextSearchCapability`/`ResultSetEquivalentToBruteForce`)
- [x] Test (integration, Postgres): `index_text`/`search_text` regex + fulltext round-trip, `delete_text_artifacts` (by content_hash and retired params_hash), `has_text_artifacts` (S2) — in `tests/integration/test_postgres_metadata.py::TestPostgresNativeTextSearch`; skips cleanly without Docker

## Find Predicates

- [x] Implement the `find_predicates` type (name, size_min/size_max, mtime_after/before, type) and conjunctive matching in `DefaultSearchProvider` find
- [x] Test: name-only find is backward compatible (`FindSearchPredicates`/`FindByNamePatternUnchanged`)
- [x] Test: size range, mtime, type, and conjunctive predicates (`FindSearchPredicates`/`FindBySizeRange`, `FindByModifiedTime`, `FindByType`, `FindConjunctivePredicates`)

## Cold-Index Failure & Straggler Verification

- [x] Implement the straggler path: individual missing/`failed`/`unsupported`/stale artifacts verified via the guarded reader within `max_content_reads`; never excluded
- [x] Implement cold/unavailable handling: index-store error OR stragglers beyond budget → raise an actionable index-unavailable / reindex-required error (no silent partial, no unbounded reads)
- [x] Implement in-`index_text` error split: content-level (undecodable/oversized) → `failed`/`unsupported` artifact in the write txn; infrastructure error → abort the txn
- [x] Test: fresh index gives complete results with zero blob reads (`ColdIndexFailsLoud`/`FreshIndexCompleteNoBlobReads`)
- [x] Test: a bounded straggler set is verified individually and matches included (`ColdIndexFailsLoud`/`BoundedStragglersVerified`)
- [x] Test: unavailable index or over-budget stragglers fail loud with an actionable error (`ColdIndexFailsLoud`/`ColdIndexFailsLoud`)
- [x] Test: non-UTF-8 content yields an `unsupported` artifact in the write txn; write succeeds (`ColdIndexFailsLoud`/`UndecodableContentIsUnsupported`)

## Reindex Lifecycle

- [x] Implement `vfs.reindex(namespace, scope)` batch backfill over current versions in scope (via `index_text`)
- [x] Implement VFS-owned bounded lazy backfill after straggler verification, persisting under the write-time CAS check (capability never self-triggers; cold → reindex, not lazy)
- [x] Implement rollback `search_meta` copy when the new version reuses a prior `content_hash` (resolves via content-addressed record)
- [x] Test: batch reindex updates `search_meta` with external artifacts for all in-scope files (`SearchMetaReindex`/`BatchReindex`)
- [x] Test: bounded lazy backfill writes only via the VFS, under CAS (`SearchMetaReindex`/`LazyBackfillIsBoundedAndVfsOwned`)
- [x] Test: rollback reusing a content_hash copies `search_meta` and resolves to the same record, no reindex (`SearchMetaReindex`/`RollbackCopiesSearchMeta`)

## Packaging

- [x] Confirm SQLite FTS5 availability (stdlib `sqlite3`); the Postgres `NativeTextSearch` implementation and any extra ride the `phase2-storage` `postgres` extra
- [x] Update `CHANGELOG.md` under Unreleased: `NativeTextSearch` capability, `SearchArtifact` envelope, guarded reader, `SearchRequest`/`SearchResponse` protocol break; note bloom not pursued, Mongo regex/fulltext and brute-force scope limiting deferred
