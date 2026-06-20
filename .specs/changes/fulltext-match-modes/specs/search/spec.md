# Search — Delta Spec

> Change: `fulltext-match-modes`
> Date: 2026-06-14 (scope expanded 2026-06-15)

## ADDED Requirements

### Requirement: FulltextWordRepresentation

> Serves: US-1

The system SHALL maintain a **word-tokenized** representation for FULLTEXT search, distinct from the **trigram** representation used for REGEX.
FULLTEXT matching SHALL use non-stemming word tokens (SQLite FTS5 `unicode61`; PostgreSQL `'simple'` text-search config), so per-term presence is exact — there SHALL be no minimum token length, and a term such as `s3` SHALL be representable.
REGEX SHALL continue to use the trigram representation unchanged.
The word-tokenized representation SHALL be derived from the stored searchable text (`raw_text`) and SHALL NOT require reading the blob store to build.
These word-token semantics are defined over fresh (`ready`) records; for content lacking a fresh index, the in-process straggler verification is a best-effort approximation (whole-word membership rather than exact `unicode61`/`'simple'` tokenization) and MAY diverge.

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

> Serves: US-1

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

## MODIFIED Requirements

### Requirement: SearchProviderProtocol

> Serves: US-1
>
> Previously: the requirement enumerated the fields `SearchRequest` carries without a
> match-mode field. This change adds `match_mode` as a public field of `SearchRequest`.

The `SearchProvider.search()` method SHALL accept a `SearchRequest` and return a
`SearchResponse`. `SearchRequest` SHALL carry the query, scope, search type, permission-pruned
`search_metas` (current-version entries for files in scope), a guarded `read_content` reader,
`SearchLimits` (the `max_content_reads` ceiling), a `find_predicates` value, and an optional
`match_mode` (`FullTextMatchMode`, default `ALL`) that applies only to FULLTEXT and is ignored
for other search types (see `FulltextMatchMode`).

### Requirement: NativeTextSearchCapability

> Serves: US-1, US-2
>
> Previously: `search_text` received a `SearchRequest` with no match-mode field; FULLTEXT
> borrowed the trigram representation (SQLite) or an English-stemmed `tsvector` (Postgres)
> and always used strict-AND; the brute-force equivalence requirement did not specify a mode
> and no cross-backend FULLTEXT equivalence was claimed.

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
