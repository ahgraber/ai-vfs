# Search — Delta Spec

> Change: `fulltext-match-modes`
> Date: 2026-06-14

## ADDED Requirements

### Requirement: FulltextMatchMode

The system SHALL support a `FullTextMatchMode` that callers may supply on a FULLTEXT search to select between strict-AND matching (`ALL`: every query term must appear in a document) and ranked-OR matching (`ANY`: at least one query term must appear, ranked by descending relevance).
The default mode SHALL be `ALL`, preserving the existing behavior for all callers that do not supply a mode.
The mode SHALL be ignored for non-FULLTEXT search types (GLOB, FIND, REGEX); specifying it for those types SHALL NOT raise an error.

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

## MODIFIED Requirements

### Requirement: NativeTextSearchCapability

> Previously: `search_text` received a `SearchRequest` with no match-mode field, so
> FULLTEXT always used strict-AND semantics on both backends; the brute-force equivalence
> requirement did not specify a mode.

The metadata store MAY expose an optional `NativeTextSearch` capability (obtained via `native_text_search()`, returning the capability or `None`) with `index_text`, `search_text`, and `delete_text_artifacts` operations.
The capability SHALL store searchable text keyed by `(provider_key, params_hash, content_hash)` — content is the searchable document; a file version is an occurrence of that content at a path.
On a search, the capability SHALL match content according to the `match_mode` carried by `SearchRequest` (see `FulltextMatchMode`) and expand each match through the permission-pruned visible versions that reference that content, emitting one result per visible occurrence with the occurrence's path and version number; result identity SHALL come from the VFS-enumerated visible version, never from fields stored on the text record.
For fresh artifacts (`ready`, current, record present), verification SHALL run against the stored text and SHALL NOT read the blob store.
For the same exact query and the same match mode, the capability SHALL return the same set of matching paths as the brute-force baseline.

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
- **THEN** all documents matching at least one query term are returned, ranked so documents
  matching more or rarer terms appear before those matching fewer

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

#### Scenario: AnyModeResultSetEquivalentAcrossBackends

- **GIVEN** the same corpus indexed by the SQLite and Postgres `NativeTextSearch`
  implementations
- **WHEN** the same exact FULLTEXT query runs against each in mode=ANY
- **THEN** both backends return the identical set of matching paths (exact scores differ
  between BM25 and ts_rank; set membership and monotonic relevance ordering are asserted,
  not score parity)
