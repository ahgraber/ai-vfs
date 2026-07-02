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

Patterns SHALL be matched line-by-line with a **linear-time** engine (RE2), so `^`/`$` anchor to line bounds and no pattern can exhibit catastrophic (super-linear) backtracking — regex content search is reachable by untrusted sandboxed code via `grep`, so an adversarial pattern MUST NOT be able to wedge the host event loop.
Consequently, patterns using features RE2 does not implement (backreferences, lookaround) SHALL be treated as unusable and yield an empty result set rather than raising or falling back to a backtracking engine.
This engine SHALL be used uniformly across every backend's in-process verification, so REGEX results are identical across backends (no backend applies a whole-document anchor-sensitive prune that could differ from per-line matching).

#### Scenario: GrepMatchesContent

- **GIVEN** file /src/main.py contains "# TODO: fix this" on line 3
- **WHEN** a principal searches with regex "TODO" in scope /src/
- **THEN** a result with path=/src/main.py, line_number=3, and match_context containing "fix this" is returned

#### Scenario: GrepNoMatch

- **GIVEN** no files in scope contain the pattern
- **WHEN** a principal searches with regex "NONEXISTENT"
- **THEN** an empty result list is returned

#### Scenario: RegexIsLinearTime

- **GIVEN** an adversarial catastrophic-backtracking pattern such as `(a+)+$` and content that does not match it
- **WHEN** a principal (or sandboxed `grep`) searches with that pattern
- **THEN** the search completes in linear time without blocking, returning no match — it does not hang

### Requirement: PluggableSearchProviders

The system SHALL dispatch each search by capability.
Glob and find (metadata-only) SHALL always be served by the `DefaultSearchProvider`.
For regex and fulltext, when the active metadata store exposes the `NativeTextSearch` capability the VFS SHALL use it.
When the store does not expose `NativeTextSearch`:

- **REGEX**: falls back to `DefaultSearchProvider` brute-force via the guarded reader; `max_content_reads` is enforced so large-scope regex fails loud (`ReadBudgetExceededError`) rather than issuing unbounded blob reads.
- **FULLTEXT**: raises `SearchTypeUnsupportedError` — no brute-force equivalent exists for unranked full-text search.

The availability and representation per backend SHALL be:

| Search type  | SQLite                                       | PostgreSQL                                | MongoDB                                         |
| ------------ | -------------------------------------------- | ----------------------------------------- | ----------------------------------------------- |
| **GLOB**     | metadata-only (`DefaultSearchProvider`)      | metadata-only                             | metadata-only                                   |
| **FIND**     | metadata-only                                | metadata-only                             | metadata-only                                   |
| **REGEX**    | native — trigram FTS5 (`search_fts`)         | native — `pg_trgm` GIN                    | brute-force via guarded reader (budget-bounded) |
| **FULLTEXT** | native — `unicode61` word index, `ALL`/`ANY` | native — `'simple'` tsvector, `ALL`/`ANY` | unsupported (`SearchTypeUnsupportedError`)      |
| **SEMANTIC** | unsupported (`ValueError`, future)           | unsupported (future)                      | unsupported (future)                            |

GLOB and FIND never read the blob store on any backend.
Native REGEX/FULLTEXT serve fresh artifacts with zero blob reads and fail loud (`ReindexRequiredError`) over a stale index rather than degrade (see `ColdIndexFailsLoud`).
FULLTEXT `match_mode` (default `ALL`) applies only where FULLTEXT is supported (see `FulltextMatchMode`).
The MongoDB column is the general "no `NativeTextSearch` capability" behavior; any store lacking the capability dispatches identically.

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

The `SearchProvider.search()` method SHALL accept a `SearchRequest` and return a
`SearchResponse`. `SearchRequest` SHALL carry the query, scope, search type, permission-pruned
`search_metas` (current-version entries for files in scope), a guarded `read_content` reader,
`SearchLimits` (the `max_content_reads` ceiling), a `find_predicates` value, and an optional
`match_mode` (`FullTextMatchMode`, default `ALL`) that applies only to FULLTEXT and is ignored
for other search types (see `FulltextMatchMode`).
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
An artifact SHALL be usable (answerable directly from the index) only when its `status` is `ready`, its `content_hash` equals the version's `content_hash`, and its `params_hash` equals the active provider's.
An identity-current artifact (matching `content_hash` and `params_hash`) is **decided**: a `ready` artifact answers from the stored text, while an `unsupported` or `failed` artifact is a confirmed non-match (binary or un-indexable content cannot satisfy a text predicate) and is excluded without further work.
An artifact that is absent or whose identity has drifted is a **straggler**: the index cannot vouch for the version, so the native search fails loud (see `ColdIndexFailsLoud`).
Because the native text record lives in the metadata store and is content-addressed, an identity-current artifact's record is always present — there is no separate external-record readability check.
The provider owns the `payload`/`artifact_ref` contents; the VFS reasons only over the common fields.

#### Scenario: ReadyArtifactUsable

- **GIVEN** a `ready` artifact whose `content_hash` and `params_hash` match the version and active provider
- **WHEN** a search consults it
- **THEN** the artifact is used to answer from the index

#### Scenario: ContentHashMismatchIsStale

- **GIVEN** an artifact whose `content_hash` differs from its version's `content_hash`
- **WHEN** a search consults it
- **THEN** the artifact is treated as a straggler (the native search fails loud per `ColdIndexFailsLoud`), not as a non-match

#### Scenario: ParamsHashMismatchIsStale

- **GIVEN** a `ready` artifact whose `params_hash` differs from the active provider's
- **WHEN** a search consults it
- **THEN** the artifact is treated as a straggler (the native search fails loud per `ColdIndexFailsLoud`), not as a non-match

### Requirement: GuardedContentReader

The VFS SHALL provide a guarded `read_content` reader rather than a bare callable, used only by the brute-force fallback search path (REGEX when no `NativeTextSearch` capability is present); the native capability path performs zero blob reads and never uses the reader.
The reader SHALL return the content of the **enumerated version** for a path (by its `content_hash`), never a later version, so verification is immune to writes that occur after enumeration.
The reader SHALL enforce `SearchLimits.max_content_reads` as a hard ceiling, raising `ReadBudgetExceededError` when it is exceeded.
The reader SHALL refuse paths outside the permission-pruned scope.

#### Scenario: ReadsEnumeratedVersionNotLatest

- **GIVEN** a file enumerated for a brute-force search at version 5 (content_hash X)
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
The capability SHALL match content using the representation appropriate to the search type — **trigram** for REGEX, **word tokens** for FULLTEXT (see `FulltextWordRepresentation`) — and, for FULLTEXT, according to the `match_mode` carried by `SearchRequest` (see `FulltextMatchMode`).
It SHALL expand each match through the permission-pruned visible versions that reference that content, emitting one result per visible occurrence with the occurrence's path and version number; result identity SHALL come from the VFS-enumerated visible version, never from fields stored on the text record.
For fresh artifacts (`ready`, current, record present), verification SHALL run against the stored text and SHALL NOT read the blob store.
For the same exact REGEX query and mode, the capability SHALL return the same set of matching paths as the brute-force baseline.
The SQLite and Postgres implementations SHALL present a **coherent FULLTEXT capability model** — non-stemming word-token matching, `ALL`/`ANY` modes, and valid sub-trigram terms — so the same query behaves consistently across backends.
Result sets are NOT guaranteed byte-identical: where tokenizer implementations differ (diacritic folding, URL/email/host segmentation), matching paths MAY differ.
For portable terms over fresh-indexed content (whole words both tokenizers segment identically) the implementations SHOULD agree, and tests MAY assert agreement for such corpora as a sanity check, not as a guaranteed contract.

#### Scenario: IndexOnWriteProducesExternalArtifact

- **GIVEN** a metadata store with `NativeTextSearch` active
- **WHEN** a text file is written
- **THEN** a content-addressed text record is upserted and a `ready` `external`
  `SearchArtifact` is stored at the provider key, within the version's write transaction

#### Scenario: AcceleratedRegexAvoidsBlobReads

- **GIVEN** 1000 fresh-indexed files where the query matches 5
- **WHEN** a regex search runs
- **THEN** the 5 matching files are returned and the guarded reader performs zero blob
  reads (verification used the stored text)

#### Scenario: RankedFulltextAllMode

- **GIVEN** indexed files of varying relevance to a fulltext query, searched with mode=ALL
  (or with no mode supplied, which defaults to ALL)
- **WHEN** a principal searches with type=FULLTEXT
- **THEN** only documents containing every query term are returned, ranked by lexical
  relevance

#### Scenario: RankedFulltextAnyMode

- **GIVEN** indexed files where some match all query terms and others match only a subset,
  searched with mode=ANY
- **WHEN** a principal searches with type=FULLTEXT
- **THEN** all documents matching at least one query term are returned, ordered by descending
  backend relevance score (BM25 / `ts_rank`); on this controlled corpus the document matching
  more (or rarer) terms ranks ahead of one matching fewer (the ordering is asserted on the
  corpus, not as a universal monotonic guarantee across arbitrary corpora)

#### Scenario: ContentMatchExpandsToVisibleOccurrences

- **GIVEN** identical content at /a.py and /b.py (same `content_hash`), both visible and
  indexed
- **WHEN** a regex matching that content runs
- **THEN** both /a.py and /b.py are returned (one content match → all visible occurrences)

#### Scenario: IdentityFromVisibleVersionAfterRollback

- **GIVEN** version N+1 created by rollback reuses version 3's `content_hash` and copied
  its `external` artifact
- **WHEN** a search matches that content
- **THEN** the result reports version N+1's path and version number (the visible
  occurrence), not version 3's

#### Scenario: ResultSetEquivalentToBruteForce

- **GIVEN** the same corpus indexed by the SQLite and Postgres `NativeTextSearch`
  implementations and searched by the brute-force baseline
- **WHEN** the same exact regex query runs against each in mode=ALL
- **THEN** all three return the identical set of matching paths

#### Scenario: AllModeCoherentAcrossBackends

- **GIVEN** the same corpus indexed by the SQLite and Postgres `NativeTextSearch`
  implementations, using portable terms (whole words both tokenizers segment identically)
- **WHEN** the same exact FULLTEXT query runs against each in mode=ALL
- **THEN** both backends apply the same word-token ALL semantics; for the portable-term
  corpus they return the same set of matching paths (asserted as a sanity check, not as a
  guaranteed-identical contract for arbitrary input)

#### Scenario: AnyModeCoherentAcrossBackends

- **GIVEN** the same corpus indexed by the SQLite and Postgres `NativeTextSearch`
  implementations, using portable terms
- **WHEN** the same exact FULLTEXT query runs against each in mode=ANY
- **THEN** both backends apply the same word-token ANY (ranked-union) semantics; for the
  portable-term corpus they return the same set of matching paths, and each backend
  independently ranks a document matching more query terms above one matching fewer (set
  membership for the portable corpus and per-backend monotonic ordering are asserted; exact
  scores, which differ between BM25 and `ts_rank`, are not)

### Requirement: ColdIndexFailsLoud

The system SHALL serve searches over a fresh native index with complete results and no blob reads.
A fresh native index is **authoritative**: every in-scope version is decided (a `ready` artifact answers from stored text; an identity-current `unsupported`/`failed` artifact is a confirmed non-match), so results are complete without reading the blob store.
**Confirmed non-match**: a file whose `unsupported` or `failed` artifact has the same `content_hash` and `params_hash` as the current version cannot satisfy a text predicate (binary or otherwise un-indexable content); it SHALL be excluded from results without further work.
**Any straggler fails loud**: if any in-scope version is a straggler — its artifact is absent, or its `content_hash`/`params_hash` has drifted — the native search SHALL fail loud with `ReindexRequiredError`, naming an actionable (path-scoped) `reindex`.
The native search SHALL NOT verify stragglers by reading the blob store, approximate them, or return partial results; a fresh index is authoritative and a stale one is repaired by `reindex`, not at query time.
When the index store itself errors during the capability call, the search SHALL fail loud with `IndexUnavailableError`.
During `index_text`, content-level errors (undecodable, oversized) SHALL produce a `failed`/`unsupported` artifact within the write transaction (the write succeeds); infrastructure errors SHALL abort the write transaction.

#### Scenario: FreshIndexCompleteNoBlobReads

- **GIVEN** every file in scope has a fresh native artifact
- **WHEN** a regex search runs
- **THEN** results are complete and no blob reads occur

#### Scenario: AnyStragglerFailsLoud

- **GIVEN** at least one in-scope version whose artifact is missing or identity-drifted while the rest are fresh
- **WHEN** a search runs (REGEX or FULLTEXT)
- **THEN** it fails with `ReindexRequiredError` naming a path-scoped `reindex` — no blob reads, no partial results, no approximation

#### Scenario: DecidedNonMatchExcluded

- **GIVEN** an in-scope version with an identity-current `unsupported` or `failed` artifact (binary or un-indexable content) alongside fresh files
- **WHEN** a search runs
- **THEN** the decided non-match is excluded from results and does NOT trigger fail-loud; the fresh files are served with no blob reads

#### Scenario: IndexUnavailableFailsLoud

- **GIVEN** the native index store errors during the capability search call
- **WHEN** a search runs
- **THEN** it fails with `IndexUnavailableError` — not a silent partial result and not a fallback blob-read storm

#### Scenario: UndecodableContentIsUnsupported

- **GIVEN** a file with non-UTF-8 content
- **WHEN** native indexing runs on write
- **THEN** an `unsupported` `SearchArtifact` is stored within the write transaction, the write succeeds, and a warning is logged

### Requirement: FulltextWordRepresentation

The system SHALL maintain a **word-tokenized** representation for FULLTEXT search, distinct from the **trigram** representation used for REGEX.
FULLTEXT matching SHALL use non-stemming word tokens (SQLite FTS5 `unicode61`; PostgreSQL `'simple'` text-search config), so per-term presence is exact — there SHALL be no minimum token length, and a term such as `s3` SHALL be representable.
REGEX SHALL continue to use the trigram representation unchanged.
The word-tokenized representation SHALL be derived from the stored searchable text (`raw_text`) and SHALL NOT require reading the blob store to build.
These word-token semantics are defined over fresh (`ready`) records; content lacking a fresh index is a straggler, and the native search fails loud (`ReindexRequiredError`) rather than approximating it in-process — there is no in-process FULLTEXT approximation (see `ColdIndexFailsLoud`).

#### Scenario: ShortTermFulltextIsRepresentable

- **GIVEN** an indexed corpus with one document "deploy to s3" and one "deploy to archive"
- **WHEN** a FULLTEXT search is run with query "s3" (mode=ALL or ANY)
- **THEN** only the "deploy to s3" document is returned; the two-character term `s3` matches
  exactly rather than degenerating to an empty constraint

#### Scenario: FulltextMatchesWholeWordsNotSubstrings

- **GIVEN** a fresh-indexed document containing the word "category"
- **WHEN** a FULLTEXT search is run with query "cat"
- **THEN** the document is NOT returned (fulltext matches whole word tokens, not substrings)

#### Scenario: RegexStillMatchesSubstrings

- **GIVEN** an indexed document containing the word "category"
- **WHEN** a REGEX search is run with pattern "cat"
- **THEN** the document IS returned (regex retains substring matching via the unchanged
  trigram representation)

### Requirement: FulltextMatchMode

The system SHALL support a `FullTextMatchMode` that callers may supply on a FULLTEXT search to select between strict-AND matching (`ALL`: every query term must appear in a document) and ranked-OR matching (`ANY`: at least one query term must appear, ranked by descending relevance).
The default mode SHALL be `ALL`, preserving the existing match-mode behavior for all callers that do not supply a mode.
The mode SHALL be ignored for non-FULLTEXT search types (GLOB, FIND, REGEX); specifying it for those types SHALL NOT raise an error.

The system SHALL reject a FULLTEXT query whose whitespace-split term count exceeds a fixed maximum (128) at the public search boundary (`vfs.VFS.search`), raising a clear error rather than constructing unbounded backend queries.
The bound is well below the PostgreSQL bind-parameter ceiling (~32767) that ANY's per-term `plainto_tsquery` construction would otherwise approach.

#### Scenario: FulltextMatchAllRequiresEveryTerm

- **GIVEN** an indexed corpus containing one document with text "hello world" and another
  with text "hello s3 bucket"
- **WHEN** a FULLTEXT search is run with query "hello s3" and mode=ALL
- **THEN** only the "hello s3 bucket" document is returned; the "hello world" document (which
  lacks the term "s3") is absent

#### Scenario: FulltextMatchAnyRanksUnion

- **GIVEN** an indexed corpus containing one document with text "hello world" (matches one
  term) and one with text "hello s3 bucket" (matches both terms)
- **WHEN** a FULLTEXT search is run with query "hello s3" and mode=ANY
- **THEN** both documents are returned; the document matching both terms ranks above the
  document matching only one term

#### Scenario: FulltextMatchModeDefaultIsAll

- **GIVEN** a caller that invokes FULLTEXT search without supplying a match mode
- **WHEN** a query is run where strict-AND and ranked-OR would return different result sets
- **THEN** only documents containing every query term are returned (ALL semantics)

#### Scenario: MatchModeIgnoredForNonFulltext

- **GIVEN** a caller supplies mode=ANY alongside a GLOB, FIND, or REGEX search type
- **WHEN** the search runs
- **THEN** no error is raised and results are identical to the same search without a mode
  argument

#### Scenario: FulltextRejectsTooManyTerms

- **GIVEN** a FULLTEXT query whose whitespace-split term count exceeds the maximum (128)
- **WHEN** the search runs
- **THEN** a clear error is raised at the public boundary before any backend query is built;
  a query at or below the maximum succeeds

> **Migration / index build** is specified in the versioning and storage deltas, not here, to
> keep the lifecycle concern in one place: the word index is (re)built from the stored
> `raw_text` at init via an idempotent, crash-resumable anti-join with **no** `params_hash`
> change and **no** blob reads (versioning `DerivedIndexRebuild`); the two-representation
> storage model is in storage `NativeTextSearchStorage`. On Postgres there is no migration —
> the `tsvector` is computed inline, so `'english'`→`'simple'` is a query-only change.
