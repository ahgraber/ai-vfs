# Tasks: object-store-text-index

> Build-dependency order: substrate → protocol → provider → VFS wiring → versioning → GC → integration.
> Each SHALL is paired with at least one evidence-producing test; foreseeable write-sites
> (synchronous index, deferred materialize-backfill, lazy straggler-backfill, rollback-copy,
> compaction-rewrite, manifest CAS publish) each get their own test task.

## Index Substrate — storage (`ObjectStoreTextIndexSubstrate`)

- [ ] Define the immutable segment object model: term dictionary + per-term postings (`content_hash` list) + per-document term frequency + per-document token length, serialized to bytes
- [ ] Implement segment write: content-address by BLAKE3 of segment bytes, store via `BlobStore.put` under a reserved key prefix, enforce a configured byte budget (`text_index_segment_max_bytes`) at seal time
- [ ] Implement segment read: whole-object `BlobStore.get` + deserialize (no range reads)
- [ ] Add the live-segment manifest persisted in the metadata store keyed by `(namespace_id, params_hash)`, with a CAS update (`MetadataCASSemantics`) on a manifest version counter; implement on SQLite, Postgres, and Mongo adapters
- [ ] Add config gating: `text_index_enabled` (default off) + `text_index_segment_max_bytes` on `VFSConfig`; resolve the index substrate only when enabled
- [ ] Test: a sealed segment round-trips whole (write→read→deserialize) and is keyed by its own byte hash under the reserved prefix (`SegmentsAreContentAddressedAndSizeBounded`)
- [ ] Test: a segment exceeding the byte budget is rejected/split at seal time (`SegmentsAreContentAddressedAndSizeBounded`)
- [ ] Test: manifest CAS update succeeds on a matching version and raises on mismatch, on each of SQLite/Postgres/Mongo (`ManifestInMetadataStoreUnderCas`)
- [ ] Test: the manifest is never written to the `BlobStore` (assert no reserved-prefix manifest object) (`ManifestInMetadataStoreUnderCas`)
- [ ] Test: with `text_index_enabled` unset, no substrate is provisioned (`DisabledByDefault`)

## Protocol Evolution — search foundation (`NativeTextSearchCapability`)

- [ ] Add `supported_search_types: set[SearchType]` to the `NativeTextSearch` protocol and declare it on the SQLite/Postgres capabilities as `{REGEX, FULLTEXT}`
- [ ] Add the indexing-discipline distinction: a `discipline` marker (`synchronous` | `deferred`) and a `materialize(namespace_id, params_hash)` method (no-op default for synchronous capabilities)
- [ ] Specify `artifact_ref` as the logical `(provider_key, params_hash, content_hash)` identity in the protocol docstring and the `SearchArtifact` external-storage contract; define a resolver that checks liveness (metadata row for synchronous; content present in a live segment for the object index)
- [ ] Test: SQLite/Postgres capabilities declare `{REGEX, FULLTEXT}` and `synchronous` discipline; `materialize` is a no-op (`SynchronousIndexOnWriteProducesExternalArtifact`)
- [ ] Test: an `external` artifact whose logical content reference is unresolvable is treated as a straggler, not a non-match (`LogicalRefSurvivesCompaction` setup; reuses `ExternalRecordMissingOrMismatchedIsStale`)

## Object-Store Provider (`ObjectStoreNativeTextSearch`)

- [ ] Implement `ObjectStoreTextIndex` declaring `supported_search_types={FULLTEXT}` and `deferred` discipline, constructed with a `BlobStore` + metadata-store handle
- [ ] Implement the tokenizer/normalization (Unicode word-split + lowercase, no stemming/stopwords) and fold its ruleset + BM25 params into `params_hash`
- [ ] Implement `index_text` (deferred): stage decoded UTF-8 text into a pending set keyed by `(params_hash, content_hash)`; record an `unsupported` artifact for non-UTF-8 content without staging it
- [ ] Implement `materialize`: seal staged content into an immutable segment (cross-segment dedup by `content_hash`), then publish in order **write segment → CAS manifest → backfill `external` artifacts**
- [ ] Implement `search_text` for FULLTEXT: read manifest → read live segments whole → BM25 score (k1=1.2, b=0.75; N/avgdl over distinct live content hashes; dedup by `content_hash`) → expand each content match through `visible_version_ids` with VFS-enumerated identity
- [ ] Implement the unsealed-straggler path: versions lacking a live-segment entry are returned to the VFS as stragglers (verified via the guarded reader), and a straggler match is appended after ranked results with a sentinel score
- [ ] Implement `delete_text_artifacts` / compaction support: drop fully-orphaned segments and rewrite partially-orphaned segments without orphaned postings
- [ ] Test: BM25 ranking order is correct over sealed content, computed from whole-segment reads with zero blob reads (`RankedFulltextFromWholeSegments`, `RankedFulltext`)
- [ ] Test: a query matching 5 of 1000 sealed files performs zero guarded-reader blob reads (`SealedContentNoBlobReads`, `AcceleratedSearchAvoidsBlobReads`)
- [ ] Test: shared content at two paths returns both occurrences from one content match (`ContentMatchExpandsToVisibleOccurrences`)
- [ ] Test: for sealed content, the matching-occurrence set equals tokenize-the-stored-text-directly, independent of rank order (result-set correctness)
- [ ] Test: non-UTF-8 content yields an `unsupported` artifact and is absent from the inverted index (`UndecodableContentUnsupported`)
- [ ] Test (write-site: materialize-backfill): after `materialize`, each in-scope version has a `ready` `external` artifact with a logical content reference (`DeferredIndexingStagesUntilSealed` sealed half)
- [ ] Test (write-site: manifest CAS): two concurrent `materialize` passes both end with their segments live; the CAS loser retries and no segment is dropped (`ConcurrentPublishNoLostSegment`)
- [ ] Test (write-site: compaction-rewrite): compacting a segment preserves usability of unchanged versions' artifacts without modifying them (`LogicalRefSurvivesCompaction`)

## VFS Provisioning & Dispatch — search (`DecoupledNativeTextSearchProvisioning`, `PluggableSearchProviders`)

- [ ] Resolve a single active `NativeTextSearch` at VFS construction: `self._meta.native_text_search()` if present, else a configured `ObjectStoreTextIndex`, else none; store it on `self._nts`
- [ ] Replace `self._meta.native_text_search()` call sites (write, search, reindex) with the resolved `self._nts`
- [ ] Dispatch by `supported_search_types`: route REGEX/FULLTEXT to `self._nts` only for declared types; undeclared REGEX → `DefaultSearchProvider` brute-force; undeclared/absent FULLTEXT → `SearchTypeUnsupportedError`
- [ ] Branch the write path on discipline: synchronous → in-transaction `index_text` (existing); deferred → stage only (no in-transaction artifact)
- [ ] Test: store-FTS present + object index configured → store capability active, object index never consulted (`StoreFtsWinsWhenBothConfigured`)
- [ ] Test: Mongo (no FTS) + object index configured → object index active (`ObjectIndexActiveWhenStoreLacksFts`)
- [ ] Test: Mongo + no object index → no native capability; FULLTEXT raises `SearchTypeUnsupportedError` (`NoNativeCapabilityWhenNeither`, `FulltextUnsupportedWithoutAnyNativeCapability`)
- [ ] Test: object index active → FULLTEXT served (ranked), REGEX falls back to bounded brute-force (`FulltextServedByObjectStoreIndex`, `UndeclaredRegexFallsBackToBruteForce`)
- [ ] Test: store-FTS capability serves both declared types (`NativeCapabilityServesDeclaredTypes`); glob/find always metadata-only (`GlobFindAlwaysAvailable`)
- [ ] Test (write-site: deferred write): a write under the object index stages text, writes no fresh artifact in the version transaction, and leaves the version a straggler (`DeferredIndexingStagesUntilSealed` staged half, `FreshWriteIsStragglerUntilMaterialized`)
- [ ] Test: a small unsealed set (≤ `max_content_reads`) is verified via the guarded reader and matches included (`UnsealedVersionIsStraggler`)
- [ ] Test: an unsealed set > `max_content_reads`, or an unreadable manifest/segment, fails loud with reindex-required (`ManuallyMaterializedColdIndexFailsLoud`)

## Versioning — reindex & backfill (`SearchMetaReindex`)

- [ ] Extend `reindex` so that for a deferred capability it calls `materialize` (seal → CAS-publish → backfill) over the scope instead of per-file in-transaction `index_text`
- [ ] Preserve synchronous reindex (per-file `index_text` in transaction) for SQLite/Postgres
- [ ] Preserve rollback `search_meta` copy; confirm the copied logical `external` reference resolves under the object index
- [ ] Test: deferred reindex seals, CAS-publishes the manifest, then backfills artifacts — in that order (`DeferredReindexSealsThenCasPublishesThenBackfills`)
- [ ] Test: synchronous batch reindex still updates `search_meta` with external artifacts (`BatchReindex`)
- [ ] Test (write-site: rollback-copy): rollback reusing a prior `content_hash` copies `search_meta` and resolves to the same sealed content — no reindex (`RollbackCopiesSearchMeta`)
- [ ] Test (write-site: lazy backfill): bounded straggler verification MAY call `index_text` and persist under CAS without the capability self-triggering (`LazyBackfillIsBoundedAndVfsOwned`)

## GC — index erasure (`ObjectStoreTextIndexSubstrate` sweep)

- [ ] Implement the index sweep: enumerate live segments per partition, drop fully-orphaned segments, compact partially-orphaned segments (rewrite without orphaned postings), drop retired-`params_hash` segments; CAS-republish the manifest
- [ ] Exclude index segment objects from the blob-orphan enumeration/sweep
- [ ] Test: deleting content then running GC removes the orphaned term from every live segment — the term no longer resolves (`PartiallyOrphanedSegmentCompactedForErasure`, erasure)
- [ ] Test: a fully-orphaned segment is dropped and removed from the manifest (`FullyOrphanedSegmentDropped`)
- [ ] Test: retired-`params_hash` segments are dropped regardless of content liveness (`SegmentReclaimedWhenParamsRetired`)
- [ ] Test: index segment objects are not reclaimed by the blob-orphan sweep (`IndexObjectsExcludedFromBlobGc`)

## Integration

- [ ] Test (end-to-end, MongoDB metadata + local `BlobStore` object index): write text files → reindex → ranked FULLTEXT returns expected ranked occurrences with zero blob reads on the hot path
- [ ] Test: for sealed content, the object-store index's FULLTEXT matching set equals the brute-force tokenization baseline over the same corpus (cross-implementation result-set equivalence)
- [ ] Wrap concurrent `materialize`/GC tests with `pyleak` `no_task_leaks`/`no_thread_leaks` per project testing policy
- [ ] Document the operational requirement (manual materialization: run `reindex` after bulk writes; background builder is deferred) in the search/reindex docstrings and the change README
