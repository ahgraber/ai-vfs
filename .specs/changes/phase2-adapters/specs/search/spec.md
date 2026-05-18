# Search — Delta Spec

> Change: `phase2-adapters`
> Date: 2026-04-04
> Design reference: `.specs/ai-vfs-bloom-provider-design.md`

## ADDED Requirements

### Requirement: BloomSearchProvider

The system SHALL provide a bloom filter search provider (`BloomSearchProvider`) that accelerates regex and fulltext search by pre-filtering candidate files using per-file bloom filter indexes.
The provider integrates `bloom-search` as an optional dependency via composition (not inheritance).

The provider SHALL declare capabilities `{SearchType.REGEX, SearchType.FULLTEXT}`.

See `.specs/ai-vfs-bloom-provider-design.md` Section 3 for implementation details.

#### Scenario: BloomIndexOnWrite

- **GIVEN** a bloom search provider is active
- **WHEN** a file is written
- **THEN** bloom filter hashes, next_masks, and loc_masks are computed and stored
  in a standard `SearchArtifact` envelope at a bloom provider key

#### Scenario: BloomAcceleratedGrep

- **GIVEN** 1000 files in scope, bloom filter indicates 5 candidates
- **WHEN** a regex search is performed
- **THEN** only the 5 candidate files have their content read and verified

#### Scenario: UnindexedFilesAreConservativeCandidates

- **GIVEN** some files in scope lack the active bloom provider key in search_meta
- **WHEN** a bloom-accelerated search is performed
- **THEN** unindexed files are included as candidates (conservative — no false negatives)

#### Scenario: NormalizerDriftDegradation

- **GIVEN** a file's bloom artifact was built with params_hash for normalizer_id="v1"
- **WHEN** the active provider uses params_hash for normalizer_id="v2"
- **THEN** the mismatched file is treated as unindexed (forwarded as candidate),
  a warning is logged, and the search completes without error

#### Scenario: EmptyPlanFallback

- **GIVEN** a query yields no extractable n-grams (EmptyPlan)
- **WHEN** a bloom search is performed
- **THEN** all files in scope are candidates (bloom filter cannot help),
  and scope limiting applies

### Requirement: SearchErrorDegradation

The system SHALL never fail a search due to index problems.
Index errors SHALL degrade to brute-force (content verification for all candidates).
Content verification is always the final arbiter of match correctness.

#### Scenario: CorruptIndexDegrades

- **GIVEN** a file has a corrupt or unrecognizable bloom artifact payload in search_meta
- **WHEN** a search includes that file in scope
- **THEN** the file is treated as unindexed and included as a candidate;
  the search completes successfully

#### Scenario: ContentDecodeFailureDuringIndex

- **GIVEN** a file contains non-UTF-8 content
- **WHEN** the bloom provider indexes the file on write
- **THEN** a `failed` or `unsupported` `SearchArtifact` is returned, the file
  remains searchable through brute force, and a warning is logged

### Requirement: SearchScopeLimiting

The system SHALL enforce a configurable `search_brute_force_limit` on the number
of files that may be brute-force searched (read content for verification) when
bloom filtering cannot narrow candidates — either due to EmptyPlan, unindexed files,
or scope exceeding the limit after filtering.

See `.specs/ai-vfs-bloom-provider-design.md` Section 5 for the CWD-expanding strategy.

#### Scenario: ScopeLimitApplied

- **GIVEN** 10,000 files in scope, query yields EmptyPlan, `search_brute_force_limit=500`
- **WHEN** a search is performed
- **THEN** only 500 files are searched (nearest to CWD first), and the response
  indicates `scope_narrowed=True`

#### Scenario: ScopeLimitNotNeededAfterBloom

- **GIVEN** 10,000 files in scope, bloom filter narrows to 50 candidates
- **WHEN** a search is performed
- **THEN** all 50 candidates are verified (under limit), `scope_narrowed=False`

### Requirement: SearchResponseMetadata

The system SHALL return search results with metadata indicating whether scope was narrowed, the actual scope searched, and the total file count before narrowing.
This allows agents to know results may be partial and decide whether to refine.

#### Scenario: NarrowedResponseMetadata

- **GIVEN** scope was narrowed due to brute-force limit
- **WHEN** search results are returned
- **THEN** `scope_narrowed=True`, `actual_scope` reflects the narrowed scope,
  and `total_files_in_scope` reflects the full count before narrowing

### Requirement: SemanticSearchProvider

The system SHALL provide a semantic search provider that computes embedding
vectors on write and ranks results by cosine similarity on search.

#### Scenario: SemanticIndexOnWrite

- **GIVEN** a semantic search provider is active
- **WHEN** a file is written
- **THEN** embedding vectors or vector references are computed and stored in a
  standard `SearchArtifact` envelope at a semantic provider key

#### Scenario: SemanticSearch

- **GIVEN** files with semantic embeddings exist in scope
- **WHEN** a principal searches with type=SEMANTIC and a natural language query
- **THEN** results are ranked by cosine similarity to the query embedding

### Requirement: CoarseFineFilerPattern

The system SHOULD implement grep optimization using a coarse-filter/fine-filter
pattern: the search index (e.g., bloom filter) identifies candidate files,
then content verification confirms matches.

#### Scenario: CoarseFilterReducesCandidates

- **GIVEN** 1000 files in scope, bloom filter indicates 5 candidates
- **WHEN** a regex search is performed
- **THEN** only the 5 candidate files have their content read and verified

## MODIFIED Requirements

### Requirement: SearchProviderProtocol

The `SearchProvider.search()` method SHALL accept a `SearchRequest` object.
`SearchRequest` SHALL include the query, scope, search type, permission-pruned
`search_metas`, a `read_content` async callback, and search limits.
(Previously: `search()` accepted `candidates: list[FileMeta] | None`.)

Providers that do not use indexes ignore the `search_metas` dict contents.
The `read_content` callback ensures providers never access storage directly.

See `.specs/ai-vfs-bloom-provider-design.md` Section 2 for the updated protocol.

#### Scenario: DefaultProviderBackwardCompatible

- **GIVEN** the default provider (glob, find, regex)
- **WHEN** search is called with a `SearchRequest`
- **THEN** the default provider ignores search_metas contents and calls
  read_content for every file (same brute-force behavior)

#### Scenario: BloomProviderUsesSearchMetas

- **GIVEN** a bloom provider
- **WHEN** search is called with a `SearchRequest` containing bloom indexes
- **THEN** the provider deserializes bloom data, filters candidates,
  and only calls read_content for candidate files

### Requirement: PluggableSearchProviders

The system SHALL support multiple concurrent search providers, each declaring its capabilities (glob, find, regex, fulltext, semantic).
The VFS SHALL dispatch search requests to the provider with the matching capability. (Previously: only default provider scenarios were specified.)

#### Scenario: ProviderDispatch

- **GIVEN** a default provider (glob, find, regex) and a bloom provider (regex)
- **WHEN** a regex search is requested
- **THEN** the bloom provider is used (more specific acceleration)

### Requirement: SearchMetadataExtensible

The system SHALL store search artifacts per-version in a standard manifest field (JSONB in SQL, nested document in NoSQL).
The manifest SHALL map provider keys to `SearchArtifact` envelopes.
The `SearchArtifact` envelope SHALL use common lifecycle and freshness fields, while each provider owns the artifact `payload` schema or `artifact_ref`. (Previously: extensibility was defined but only empty-dict usage was demonstrated.)

### Requirement: FindSearchPredicates

The system SHALL extend `FindSearch` to support predicate-based metadata search matching file name patterns, size ranges, modification times, and content type.
(Previously, Phase 1 supported only name-pattern matching against the file basename.)

The `SearchRequest` SHALL carry a `find_predicates` field — a typed predicate object whose fields are independently optional and combined conjunctively when multiple are supplied (e.g., name pattern AND size range).
The `type` predicate distinguishes between live files and tombstones; richer typing (mime / content classification) is out of scope.

#### Scenario: FindByNamePatternUnchanged

- **GIVEN** the existing Phase 1 name-pattern behavior
- **WHEN** a principal calls find with only a name predicate
- **THEN** the result set matches the Phase 1 `FindByNamePattern` scenario (backward compatible)

#### Scenario: FindBySizeRange

- **GIVEN** files with sizes 100, 5_000, and 50_000 bytes
- **WHEN** a principal calls find with `size_min=1_000` and `size_max=10_000`
- **THEN** only the 5_000-byte file is returned

#### Scenario: FindByModifiedTime

- **GIVEN** files written at t-2h, t-1d, and t-30d (where t is the current time)
- **WHEN** a principal calls find with `mtime_after = t-24h`
- **THEN** only the t-2h file is returned

#### Scenario: FindByType

- **GIVEN** an existing live file and a tombstoned file at the same path history
- **WHEN** a principal calls find with `type="file"`
- **THEN** only the live file is returned; the tombstone is excluded

#### Scenario: FindConjunctivePredicates

- **GIVEN** files /src/a.py (small, recent), /src/b.py (large, old), /data/c.txt (small, recent)
- **WHEN** a principal calls find with `name="*.py"` AND `size_max=10_000`
- **THEN** only /src/a.py is returned

#### Scenario: MultipleProviderArtifacts

- **GIVEN** bloom and semantic providers are both active
- **WHEN** a file is written
- **THEN** search_meta contains provider-keyed `SearchArtifact` entries for
  both providers
