# Versioning — Spec

## Requirements

### Requirement: ImmutableVersionHistory

The system SHALL store every write as an immutable version record.
No version record's content fields (version_number, content_hash, size, created_at, created_by, is_tombstone, parent_version_id) SHALL ever be mutated after creation.
Only GC MAY delete version records per the retention policy.

> **Note:** `search_meta` is explicitly exempt from this immutability contract.
> It is a mutable, provider-populated indexing field updated by `update_search_meta`
> and the `SearchMetaReindex` operation below. All other fields are immutable.

#### Scenario: VersionsNeverMutated

- **GIVEN** a version record exists with version_number=3 and content_hash=X
- **WHEN** a new write creates version_number=4
- **THEN** version 3's record remains unchanged

### Requirement: RollbackCreatesNewVersion

The system SHALL implement rollback by creating a new version whose content_hash
references the target version's blob, not by mutating existing records.

#### Scenario: RollbackToVersion

- **GIVEN** a file with versions 1 (hash=A), 2 (hash=B), 3 (hash=C)
- **WHEN** a principal rolls back to version 1
- **THEN** version 4 is created with content_hash=A and parent_version_id pointing to version 1's ULID

#### Scenario: RollbackContentAccessible

- **GIVEN** a file rolled back from version 3 to version 1
- **WHEN** a principal reads the file
- **THEN** the content matches version 1's original content

### Requirement: VersionHistoryQuery

The system SHALL return version history newest-first with configurable limit and cursor-based pagination.

#### Scenario: ListVersionsNewestFirst

- **GIVEN** a file with 5 versions
- **WHEN** a principal queries versions with limit=3
- **THEN** versions 5, 4, 3 are returned in that order

### Requirement: RetentionPolicy

The system SHALL support a configurable Time Machine-style retention policy per namespace, with global defaults.
The policy SHALL be declarative data: a `RetentionPolicy` model carries `max_recent_versions`, `keep_first_version`, `keep_current_version`, and a list of `RetentionTier(max_age, keep_every)` entries describing the intended Time Machine cadence.

> **Phase 1 scope:** Phase 1 GC enforces only `max_recent_versions`, `keep_first_version`, and `keep_current_version`.
> The time-based `tiers` field is stored as declarative data — neither the metadata store adapter nor the library evaluates tier rules in Phase 1.
> Tier evaluation lands in `phase2-adapters/TierBasedRetention` and lives in the library (`GarbageCollector`), not in the store adapter; the store gains a coarse `iter_versions_for_gc` enumerator and the library applies tier semantics in a single canonical implementation.

#### Scenario: DefaultRetentionData

- **GIVEN** the `RetentionPolicy()` default constructor is invoked
- **WHEN** its fields are inspected
- **THEN** `max_recent_versions == 50`, `keep_first_version is True`, `keep_current_version is True`, and `tiers` carries the Time Machine-style default cadence (24h / 7d / 30d / beyond) as declarative data only

#### Scenario: AlwaysKeepCurrentAndFirst

- **GIVEN** a file with versions 1 through 100
- **WHEN** GC applies retention with max_recent_versions=5
- **THEN** version 1 (first) and the current version are always preserved, independent of any tier definitions stored on the policy

### Requirement: VersionGarbageCollection

The system SHALL provide a GC process that deletes version metadata records
beyond the retention policy, then deletes blob objects with zero remaining
references across all namespaces.

#### Scenario: GCDeletesOldVersions

- **GIVEN** a file with 10 versions and retention max_recent_versions=3
- **WHEN** GC runs
- **THEN** versions beyond the 3 most recent are deleted (except version 1 if keep_first_version is true)

#### Scenario: GCPreservesSharedBlobs

- **GIVEN** two files in different namespaces share the same content_hash
- **WHEN** GC deletes versions in one namespace
- **THEN** the shared blob is NOT deleted because it still has references in the other namespace

#### Scenario: GCSafeToSkip

- **GIVEN** GC has not run in 30 days
- **WHEN** the system operates normally
- **THEN** correctness is unaffected; the system accumulates versions and blobs until GC runs

### Requirement: SearchMetaReindex

The system SHALL provide a reindex operation that backfills search metadata
for files written before a search provider was activated.

#### Scenario: BatchReindex

- **GIVEN** a new search provider is activated
- **WHEN** a principal calls reindex for a namespace and scope
- **THEN** all files in scope have their search_meta updated with the provider's artifacts
