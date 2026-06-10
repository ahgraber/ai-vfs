# Search — Spec

## Requirements

### Requirement: GlobSearch

The system SHALL support glob pattern matching against file paths within a scoped directory, with optional recursion.

#### Scenario: NonRecursiveGlob

- **GIVEN** files /src/main.py, /src/utils.py, /src/tests/test_main.py
- **WHEN** a principal searches with glob pattern "\*.py" in scope /src/
- **THEN** /src/main.py and /src/utils.py are returned (not the nested file)

#### Scenario: RecursiveGlob

- **GIVEN** files /src/main.py, /src/tests/test_main.py
- **WHEN** a principal searches with glob pattern "\*\*/\*.py" in scope /src/
- **THEN** both files are returned

### Requirement: FindSearchPredicates

The system SHALL support predicate-based metadata search matching on file name pattern, size range, modification time, and live/tombstone type.
Predicates are carried by a typed `find_predicates` value on `SearchRequest` whose fields are independently optional and combined conjunctively.
Richer typing (mime / content classification) is out of scope.

#### Scenario: FindByNamePattern

- **GIVEN** files /src/main.py and /src/config.yaml
- **WHEN** a principal searches with find predicate name="\*.py"
- **THEN** only /src/main.py is returned

#### Scenario: FindBySizeRange

- **GIVEN** files with sizes 100, 5_000, and 50_000 bytes
- **WHEN** a principal calls find with `size_min=1_000` and `size_max=10_000`
- **THEN** only the 5_000-byte file is returned

#### Scenario: FindByModifiedTime

- **GIVEN** files written at t-2h, t-1d, and t-30d (t = now)
- **WHEN** a principal calls find with `mtime_after = t-24h`
- **THEN** only the t-2h file is returned

#### Scenario: FindByType

- **GIVEN** an existing live file and a tombstoned file
- **WHEN** a principal calls find with `type="file"`
- **THEN** only the live file is returned; the tombstone is excluded

#### Scenario: FindConjunctivePredicates

- **GIVEN** files /src/a.py (small, recent), /src/b.py (large, old), /data/c.txt (small, recent)
- **WHEN** a principal calls find with `name="*.py"` AND `size_max=10_000`
- **THEN** only /src/a.py is returned

### Requirement: RegexContentSearch

The system SHALL support regex pattern matching against file content,
returning matching paths with context (matched line and line number).

#### Scenario: GrepMatchesContent

- **GIVEN** file /src/main.py contains "# TODO: fix this" on line 3
- **WHEN** a principal searches with regex "TODO" in scope /src/
- **THEN** a result with path=/src/main.py, line_number=3, and match_context containing "fix this" is returned

#### Scenario: GrepNoMatch

- **GIVEN** no files in scope contain the pattern
- **WHEN** a principal searches with regex "NONEXISTENT"
- **THEN** an empty result list is returned

### Requirement: PluggableSearchProviders

The system SHALL dispatch each search by capability.
Glob and find (metadata-only) SHALL always be served by the `DefaultSearchProvider`.
For regex and fulltext, when the active metadata store exposes the `NativeTextSearch` capability the VFS SHALL use it.
When the store does not expose `NativeTextSearch`:

- **REGEX**: falls back to `DefaultSearchProvider` brute-force via the guarded reader; `max_content_reads` is enforced so large-scope regex fails loud (`ReadBudgetExceededError`) rather than issuing unbounded blob reads.
- **FULLTEXT**: raises `SearchTypeUnsupportedError` — no brute-force equivalent exists for unranked full-text search.

> **Deferred:** Whole-scope brute-force scope management (bounding regex on very large corpora
> across any backend) and semantic search are deferred to future changes.

#### Scenario: NativeCapabilityServesRegex

- **GIVEN** a metadata store exposing `NativeTextSearch`
- **WHEN** a regex search is requested
- **THEN** the VFS dispatches to the store's `search_text` (verification against stored text, no blob reads for fresh artifacts)

#### Scenario: GlobFindAlwaysAvailable

- **GIVEN** any metadata backend (with or without `NativeTextSearch`)
- **WHEN** a glob or find search is requested
- **THEN** the `DefaultSearchProvider` serves it from metadata, with no blob reads

#### Scenario: RegexFallbackToBruteForce

- **GIVEN** a metadata store without `NativeTextSearch` (e.g. MongoDB standalone)
- **WHEN** a regex search is requested
- **THEN** the VFS serves it via bounded brute-force through the `DefaultSearchProvider` + guarded reader; `max_content_reads` is enforced so large-scope regex fails loud (`ReadBudgetExceededError`)

#### Scenario: FulltextUnsupportedWithoutNativeCapability

- **GIVEN** a metadata store without `NativeTextSearch` (e.g. MongoDB)
- **WHEN** a fulltext search is requested
- **THEN** the search raises `SearchTypeUnsupportedError`

#### Scenario: UnknownCapabilityRejected

- **GIVEN** the active provider does not declare the SEMANTIC capability
- **WHEN** a principal requests a semantic search
- **THEN** a `ValueError` is raised indicating no provider supports the requested search type

### Requirement: SearchProviderProtocol

The `SearchProvider.search()` method SHALL accept a `SearchRequest` and return a `SearchResponse`.
`SearchRequest` SHALL carry the query, scope, search type, permission-pruned `search_metas` (current-version entries for files in scope), a guarded `read_content` reader, `SearchLimits` (the `max_content_reads` ceiling), and a `find_predicates` value.
`SearchProvider.index()` SHALL return a `SearchArtifact | None` (None for no-op providers).

#### Scenario: DefaultProviderMigratedToRequest

- **GIVEN** the default provider (glob, find)
- **WHEN** `search` is called with a `SearchRequest`
- **THEN** it serves glob/find from `search_metas` (metadata only) and returns a `SearchResponse`

#### Scenario: IndexReturnsArtifactOrNone

- **GIVEN** a metadata store exposing `NativeTextSearch` and the default provider
- **WHEN** a file is written
- **THEN** native indexing produces a `ready` `SearchArtifact` and the default provider's `index` returns `None`

### Requirement: SearchMetadataExtensible

The system SHALL store search artifacts per version in a standard manifest field (JSON/JSONB in SQL, subdocument in NoSQL) mapping provider keys to `SearchArtifact` envelopes.
Native text search SHALL store its searchable text in a content-addressed index record and reference it via an `external` artifact rather than embedding text in `search_meta`.

#### Scenario: ManifestReferencesExternalTextRecord

- **GIVEN** a metadata store with `NativeTextSearch` active
- **WHEN** a file is written
- **THEN** `search_meta` contains an entry at the provider key whose value is a `SearchArtifact` with `storage="external"` referencing the content-addressed text record

#### Scenario: EmptySearchMetaByDefault

- **GIVEN** only the default provider is active (no `NativeTextSearch`)
- **WHEN** a file is written
- **THEN** search_meta is an empty dict `{}`

### Requirement: SearchArtifactEnvelope

The system SHALL represent every search artifact as a `SearchArtifact` envelope carrying common lifecycle and freshness fields — `status` (one of `ready`, `failed`, `unsupported`), `schema_version`, `provider_key`, `provider_version`, `params_hash`, `content_hash`, `created_at`, `storage` (one of `inline`, `blob`, `external`), `error_code`, and `error_message` — and either an inline `payload` or an `artifact_ref`.
An artifact SHALL be usable only when its `status` is `ready`, its `content_hash` equals the version's `content_hash`, and its `params_hash` equals the active provider's.
For an `external` artifact, usability SHALL additionally require that the referenced index record is readable and that its recorded identity (`content_hash`/`params_hash`) matches; a missing, unreadable, or mismatched record SHALL be treated as a straggler (verified individually), never as a confirmed non-match.
The provider owns the `payload`/`artifact_ref` contents; the VFS reasons only over the common fields.

#### Scenario: ReadyArtifactUsable

- **GIVEN** a `ready` artifact whose `content_hash` and `params_hash` match the version and active provider
- **WHEN** a search consults it
- **THEN** the artifact is used to answer from the index

#### Scenario: ContentHashMismatchIsStale

- **GIVEN** an artifact whose `content_hash` differs from its version's `content_hash`
- **WHEN** a search consults it
- **THEN** the artifact is treated as a straggler (verified individually), not as a non-match

#### Scenario: ParamsHashMismatchIsStale

- **GIVEN** a `ready` artifact whose `params_hash` differs from the active provider's
- **WHEN** a search consults it
- **THEN** the artifact is treated as a straggler (verified individually), not as a non-match

#### Scenario: ExternalRecordMissingOrMismatchedIsStale

- **GIVEN** a `ready` `external` artifact whose referenced index record is missing, unreadable, or records a different `content_hash`/`params_hash`
- **WHEN** a search consults it
- **THEN** the artifact is treated as a straggler (verified individually) — never a confirmed non-match

### Requirement: GuardedContentReader

The VFS SHALL provide a guarded `read_content` reader rather than a bare callable, used only for the bounded straggler-verification path.
The reader SHALL return the content of the **enumerated version** for a path (by its `content_hash`), never a later version, so verification is immune to writes that occur after enumeration.
The reader SHALL enforce `SearchLimits.max_content_reads` as a hard ceiling, raising `ReadBudgetExceededError` when it is exceeded.
The reader SHALL refuse paths outside the permission-pruned scope.

#### Scenario: ReadsEnumeratedVersionNotLatest

- **GIVEN** a file enumerated for search at version 5 (content_hash X)
- **WHEN** a concurrent write creates version 6 (content_hash Y) and the path is then read for verification
- **THEN** the reader returns version 5's content (X), not version 6's

#### Scenario: BudgetCeilingEnforced

- **GIVEN** `SearchLimits.max_content_reads = 10`
- **WHEN** an 11th content read is attempted
- **THEN** the reader raises `ReadBudgetExceededError`

#### Scenario: OutOfScopePathRefused

- **GIVEN** a path not present in the request's permission-pruned `search_metas`
- **WHEN** it is requested through the reader
- **THEN** the read is refused

### Requirement: NativeTextSearchCapability

The metadata store MAY expose an optional `NativeTextSearch` capability (obtained via `native_text_search()`, returning the capability or `None`) with `index_text`, `search_text`, and `delete_text_artifacts` operations.
The capability SHALL store searchable text keyed by `(provider_key, params_hash, content_hash)` — content is the searchable document; a file version is an occurrence of that content at a path.
On a search, the capability SHALL match content and expand each match through the permission-pruned visible versions that reference that content, emitting one result per visible occurrence with the occurrence's path and version number; result identity SHALL come from the VFS-enumerated visible version, never from fields stored on the text record.
For fresh artifacts (`ready`, current, record present), verification SHALL run against the stored text and SHALL NOT read the blob store.
For the same exact query, the capability SHALL return the same set of matching paths as the brute-force baseline.

#### Scenario: IndexOnWriteProducesExternalArtifact

- **GIVEN** a metadata store with `NativeTextSearch` active
- **WHEN** a text file is written
- **THEN** a content-addressed text record is upserted and a `ready` `external` `SearchArtifact` is stored at the provider key, within the version's write transaction

#### Scenario: AcceleratedRegexAvoidsBlobReads

- **GIVEN** 1000 fresh-indexed files where the query matches 5
- **WHEN** a regex search runs
- **THEN** the 5 matching files are returned and the guarded reader performs zero blob reads (verification used the stored text)

#### Scenario: RankedFulltext

- **GIVEN** indexed files of varying relevance to a fulltext query
- **WHEN** a principal searches with type=FULLTEXT
- **THEN** results are returned ranked by lexical relevance

#### Scenario: ContentMatchExpandsToVisibleOccurrences

- **GIVEN** identical content at /a.py and /b.py (same `content_hash`), both visible and indexed
- **WHEN** a regex matching that content runs
- **THEN** both /a.py and /b.py are returned (one content match → all visible occurrences)

#### Scenario: IdentityFromVisibleVersionAfterRollback

- **GIVEN** version N+1 created by rollback reuses version 3's `content_hash` and copied its `external` artifact
- **WHEN** a search matches that content
- **THEN** the result reports version N+1's path and version number (the visible occurrence), not version 3's

#### Scenario: ResultSetEquivalentToBruteForce

- **GIVEN** the same corpus indexed by the SQLite and Postgres `NativeTextSearch` implementations and searched by the brute-force baseline
- **WHEN** the same exact regex query runs against each
- **THEN** all three return the identical set of matching paths

### Requirement: ColdIndexFailsLoud

The system SHALL serve searches over a fresh native index with complete results and no blob reads.
A bounded set of stragglers — individual files whose artifact is missing, `failed`, or stale — SHALL be verified individually via the guarded reader within `max_content_reads`, never excluded, so a fresh index produces no false negatives for what is searched.
**Exception — identity-matched `unsupported` artifact**: a file whose `unsupported` artifact has the same `content_hash` and `params_hash` as the current version IS a confirmed non-match (binary content cannot satisfy any text predicate); it SHALL be excluded from results and SHALL NOT consume straggler budget.
All other `unsupported` entries (hash drift) are treated as stragglers and verified.
A cold or unavailable index — the index store errors, or the straggler set exceeds `max_content_reads` — SHALL fail loud with an actionable error (index-unavailable or reindex-required); the system SHALL NOT silently return partial results or read content for an unbounded scope.
During `index_text`, content-level errors (undecodable, oversized) SHALL produce a `failed`/`unsupported` artifact within the write transaction (the write succeeds); infrastructure errors SHALL abort the write transaction.

#### Scenario: FreshIndexCompleteNoBlobReads

- **GIVEN** every file in scope has a fresh native artifact
- **WHEN** a regex search runs
- **THEN** results are complete and the guarded reader performs zero blob reads

#### Scenario: BoundedStragglersVerified

- **GIVEN** a few files (≤ `max_content_reads`) lack a fresh artifact while the rest are indexed
- **WHEN** a search runs
- **THEN** the stragglers are verified individually via the guarded reader and included if they match — no false negatives

#### Scenario: ColdIndexFailsLoud

- **GIVEN** the index store is unavailable, or the straggler set exceeds `max_content_reads`
- **WHEN** a search runs
- **THEN** it fails with an actionable index-unavailable / reindex-required error — not a silent partial result and not an unbounded blob-read storm

#### Scenario: UndecodableContentIsUnsupported

- **GIVEN** a file with non-UTF-8 content
- **WHEN** native indexing runs on write
- **THEN** an `unsupported` `SearchArtifact` is stored within the write transaction, the write succeeds, and a warning is logged
