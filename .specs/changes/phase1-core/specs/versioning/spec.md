# Versioning — Delta Spec

> Change: `phase1-core`
> Date: 2026-04-04

## ADDED Requirements

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

> **Phase 1 scope:** The Phase 1 GC implementation covers `max_recent_versions` and
> `keep_first_version` only. The time-based retention tiers (24h / 7d / 30d) are modelled
> in `RetentionPolicy.tiers` but are **not enforced by the GC in Phase 1**. Full tier
> evaluation is deferred to a later phase.

#### Scenario: DefaultRetention

- **GIVEN** no namespace-specific retention override
- **WHEN** the retention policy is evaluated
- **THEN** the defaults apply: max 50 recent versions; last 24h keep all; last 7d keep one per hour; last 30d keep one per day; beyond 30d keep one per week

#### Scenario: AlwaysKeepCurrentAndFirst

- **GIVEN** a file with versions 1 through 100
- **WHEN** GC applies retention with max_recent_versions=5
- **THEN** version 1 (first) and the current version are always preserved regardless of retention tier

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
