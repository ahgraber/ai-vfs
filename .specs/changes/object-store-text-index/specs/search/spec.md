# Search — Delta Spec

> Change: `object-store-text-index`
> Date: 2026-06-11

## MODIFIED Requirements

### Requirement: PluggableSearchProviders

> Previously: native text search was reachable only via the metadata store, and the VFS routed both regex and fulltext to any native capability indiscriminately; FULLTEXT raised `SearchTypeUnsupportedError` whenever the metadata store lacked `NativeTextSearch`.

The system SHALL dispatch each search by capability.
Glob and find (metadata-only) SHALL always be served by the `DefaultSearchProvider`.
For regex and fulltext, the VFS SHALL use the active `NativeTextSearch` capability **only for the search types that capability declares** in `supported_search_types`.
The active capability is resolved per `DecoupledNativeTextSearchProvisioning` — from the metadata store, or from a configured object-store index when the store exposes none.
For a requested type that the active capability does not declare (or when no native capability is active):

- **REGEX**: falls back to `DefaultSearchProvider` brute-force via the guarded reader; `max_content_reads` is enforced so large-scope regex fails loud (`ReadBudgetExceededError`) rather than issuing unbounded blob reads.
- **FULLTEXT**: raises `SearchTypeUnsupportedError` — no brute-force equivalent exists for unranked full-text search.

The object-store index declares `{FULLTEXT}` only; REGEX therefore uses bounded brute-force on a store without native FTS even when the object-store index is configured.

> **Deferred:** Whole-scope brute-force scope management, accelerated REGEX via the object-store index,
> and semantic search are deferred to future changes.

#### Scenario: NativeCapabilityServesDeclaredTypes

- **GIVEN** a metadata store whose `NativeTextSearch` declares `{REGEX, FULLTEXT}`
- **WHEN** a regex or fulltext search is requested
- **THEN** the VFS dispatches it to the store's `search_text` (verification against stored text, no blob reads for fresh artifacts)

#### Scenario: GlobFindAlwaysAvailable

- **GIVEN** any metadata backend (with or without `NativeTextSearch`)
- **WHEN** a glob or find search is requested
- **THEN** the `DefaultSearchProvider` serves it from metadata, with no blob reads

#### Scenario: UndeclaredRegexFallsBackToBruteForce

- **GIVEN** the active capability is the object-store index (declares `{FULLTEXT}`, not REGEX), or there is no native capability
- **WHEN** a regex search is requested
- **THEN** the VFS serves it via bounded brute-force through the `DefaultSearchProvider` + guarded reader; `max_content_reads` is enforced so large-scope regex fails loud (`ReadBudgetExceededError`)

#### Scenario: FulltextServedByObjectStoreIndex

- **GIVEN** a metadata store without native FTS (e.g. MongoDB) and a configured object-store index declaring `{FULLTEXT}`
- **WHEN** a fulltext search is requested
- **THEN** the VFS serves it through the object-store index, returning ranked results — not `SearchTypeUnsupportedError`

#### Scenario: FulltextUnsupportedWithoutAnyNativeCapability

- **GIVEN** a metadata store without native FTS (e.g. MongoDB) and **no** object-store index configured
- **WHEN** a fulltext search is requested
- **THEN** the search raises `SearchTypeUnsupportedError`

#### Scenario: UnknownCapabilityRejected

- **GIVEN** no active provider or capability declares the SEMANTIC capability
- **WHEN** a principal requests a semantic search
- **THEN** a `ValueError` is raised indicating no provider supports the requested search type

### Requirement: NativeTextSearchCapability

> Previously: the capability was obtained only from the metadata store, indexed exclusively in-transaction on write (returning a `ready` artifact immediately), served regex and fulltext without declaring which, and `artifact_ref` resolution was unspecified beyond "the referenced index record".

A `NativeTextSearch` capability MAY be provisioned from the metadata store (`native_text_search()`, returning the capability or `None`) **or** independently of it (see `DecoupledNativeTextSearchProvisioning`).
The capability SHALL declare `supported_search_types`; the VFS SHALL route a search type to it only when that type is declared.
The capability SHALL store searchable text keyed by `(provider_key, params_hash, content_hash)` — content is the searchable document; a file version is an occurrence of that content at a path.
On a search, the capability SHALL match content and expand each match through the permission-pruned visible versions that reference that content, emitting one result per visible occurrence with the occurrence's path and version number; result identity SHALL come from the VFS-enumerated visible version, never from fields stored on the text record.
For fresh artifacts (`ready`, current, record present), verification SHALL run against the stored text and SHALL NOT read the blob store.
For the same exact query, the capability SHALL return the same set of matching paths as the brute-force baseline.

The capability SHALL declare an **indexing discipline**:

- **Synchronous** (SQLite, Postgres): `index_text` runs in the version write transaction and returns a `ready` `external` artifact immediately.
- **Deferred** (object-store index): `index_text` stages the text but seals nothing; a `materialize` pass seals staged content and backfills artifacts.
  Until a version's content is sealed it carries no fresh artifact and is a straggler.
  `materialize` is a no-op for synchronous capabilities.

An `external` artifact's `artifact_ref` SHALL be a **stable logical reference** to the `(provider_key, params_hash, content_hash)` text identity, never a physical storage location; its liveness SHALL be that the referenced text is resolvable under the active profile (a metadata-store row for synchronous capabilities; a content hash present in a live segment for the object-store index).
An unresolvable reference SHALL be treated as a straggler, never a confirmed non-match.

#### Scenario: SynchronousIndexOnWriteProducesExternalArtifact

- **GIVEN** a synchronous capability (SQLite/Postgres) active
- **WHEN** a text file is written
- **THEN** a content-addressed text record is upserted and a `ready` `external` `SearchArtifact` is stored at the provider key, within the version's write transaction

#### Scenario: DeferredIndexingStagesUntilSealed

- **GIVEN** a deferred capability (object-store index) active
- **WHEN** a text file is written
- **THEN** the text is staged, no `ready` artifact is stored in the write transaction, and the version is a straggler until a `materialize` pass seals it and backfills the `external` artifact

#### Scenario: AcceleratedSearchAvoidsBlobReads

- **GIVEN** 1000 fresh-indexed files where the query matches 5 (any active capability, type within `supported_search_types`)
- **WHEN** a search runs
- **THEN** the 5 matching files are returned and the guarded reader performs zero blob reads (verification used the stored/sealed text)

#### Scenario: RankedFulltext

- **GIVEN** indexed files of varying relevance to a fulltext query
- **WHEN** a principal searches with type=FULLTEXT
- **THEN** results are returned ranked by lexical relevance

#### Scenario: ContentMatchExpandsToVisibleOccurrences

- **GIVEN** identical content at /a.py and /b.py (same `content_hash`), both visible and indexed
- **WHEN** a query matching that content runs
- **THEN** both /a.py and /b.py are returned (one content match → all visible occurrences)

#### Scenario: IdentityFromVisibleVersionAfterRollback

- **GIVEN** version N+1 created by rollback reuses version 3's `content_hash` and copied its `external` artifact
- **WHEN** a search matches that content
- **THEN** the result reports version N+1's path and version number (the visible occurrence), not version 3's

#### Scenario: LogicalRefSurvivesCompaction

- **GIVEN** a version whose `external` artifact references `(provider_key, params_hash, content_hash)` and whose content was sealed into segment S
- **WHEN** compaction rewrites segment S into a new segment S' (same content still live)
- **THEN** the artifact remains usable without modification — the logical reference still resolves because the content_hash is present in a live segment

## ADDED Requirements

### Requirement: DecoupledNativeTextSearchProvisioning

The VFS SHALL resolve a single active `NativeTextSearch` capability from either the metadata store (`native_text_search()`) or an independently-configured object-store index, rather than from the metadata store alone.
When the metadata store exposes native FTS, that capability SHALL be the active one and the object-store index SHALL NOT be used — so a backend that already has native FTS is never double-indexed.
When the metadata store exposes none and an object-store index is configured, the object-store index SHALL be the active capability.
When neither is available, no native capability is active.

#### Scenario: StoreFtsWinsWhenBothConfigured

- **GIVEN** a SQLite or Postgres store (native FTS) **and** a configured object-store index
- **WHEN** the active native text search capability is resolved
- **THEN** the metadata-store capability is active and the object-store index is not consulted for searching or indexing

#### Scenario: ObjectIndexActiveWhenStoreLacksFts

- **GIVEN** a MongoDB store (no native FTS) and a configured object-store index
- **WHEN** the active native text search capability is resolved
- **THEN** the object-store index is the active capability

#### Scenario: NoNativeCapabilityWhenNeither

- **GIVEN** a MongoDB store (no native FTS) and no object-store index configured
- **WHEN** the active native text search capability is resolved
- **THEN** no native capability is active and FULLTEXT is unsupported

### Requirement: ObjectStoreNativeTextSearch

The object-store index SHALL implement the `NativeTextSearch` capability as a **deferred** discipline declaring `supported_search_types = {FULLTEXT}`, persisting a term→postings inverted index (with per-document term frequencies) over decoded UTF-8 text keyed by `(provider_key, params_hash, content_hash)` into immutable size-bounded segments in the `BlobStore`, with the live-segment manifest persisted per `(namespace_id, params_hash)` in the metadata store.
A `content_hash` SHALL be indexed at most once across all live segments (cross-segment dedup).
FULLTEXT SHALL be answered by reading the manifest and the relevant **whole** segments — no range reads — ranking candidates by **BM25** (parameters fixed and folded into `params_hash`; see design), then expanding each content match through the permission-pruned visible occurrences with identity from the VFS-enumerated visible version.
For content sealed into a live segment, a search SHALL answer from segment postings and SHALL NOT read the blob store for verification.
The index is **manually materialized**: `index_text` stages content without sealing, and only a `materialize`/`reindex` pass seals it.
A version whose content is not yet sealed SHALL be treated as a straggler — verified via the guarded reader within `max_content_reads`, never excluded — and when the unsealed set exceeds the budget, or a manifest/segment is unreadable, the search SHALL fail loud with a reindex-required / index-unavailable error, never returning silent partial results or issuing unbounded blob reads.
For sealed content, the set of matching occurrences for a FULLTEXT query SHALL equal the set computed by tokenizing the stored text directly, independent of BM25 ranking order.
Undecodable (non-UTF-8) content SHALL produce an `unsupported` artifact and SHALL NOT be added to the inverted index.

#### Scenario: RankedFulltextFromWholeSegments

- **GIVEN** sealed content of varying relevance to a fulltext query
- **WHEN** a principal searches with type=FULLTEXT through the object-store index
- **THEN** results are returned ranked by BM25 relevance, computed by reading the manifest and the relevant whole segments, with zero blob reads

#### Scenario: SealedContentNoBlobReads

- **GIVEN** 1000 files whose content is sealed into live segments and a query matching 5
- **WHEN** a fulltext search runs
- **THEN** the 5 matching files are returned and the guarded reader performs zero blob reads

#### Scenario: UnsealedVersionIsStraggler

- **GIVEN** a small number (within `max_content_reads`) of just-written versions whose content is not yet sealed
- **WHEN** a fulltext search runs
- **THEN** the unsealed versions are verified individually via the guarded reader and included if they match — no false negative from index lag

#### Scenario: ManuallyMaterializedColdIndexFailsLoud

- **GIVEN** more than `max_content_reads` versions written since the last `materialize`, or an unreadable manifest/segment
- **WHEN** a broad fulltext search runs
- **THEN** it fails loud with a reindex-required / index-unavailable error — not a silent partial result and not an unbounded blob-read storm

#### Scenario: ContentMatchExpandsToVisibleOccurrences

- **GIVEN** identical content at /a.md and /b.md (same `content_hash`), both visible and sealed into a live segment
- **WHEN** a fulltext query matching that content runs
- **THEN** both /a.md and /b.md are returned (one content match → all visible occurrences)

#### Scenario: UndecodableContentUnsupported

- **GIVEN** a file with non-UTF-8 content under an active object-store index
- **WHEN** indexing runs
- **THEN** an `unsupported` `SearchArtifact` is recorded for that version and the content is not added to the inverted index
