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

#### Scenario: DefaultRetentionData

- **GIVEN** the `RetentionPolicy()` default constructor is invoked
- **WHEN** its fields are inspected
- **THEN** `max_recent_versions == 50`, `keep_first_version is True`, `keep_current_version is True`, and `tiers` carries the Time Machine-style default cadence (24h / 7d / 30d / beyond)

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

### Requirement: TierBasedRetention

The system SHALL evaluate the time-based `RetentionPolicy.tiers` field during GC.
A `RetentionTier(max_age, keep_every)` SHALL be interpreted as: for versions within `max_age` of `now`, keep the first version observed in each consecutive `keep_every` window and reclaim the rest; "first" is the version with the smallest `created_at` in the window — a deterministic rule independent of GC ordering.
A `keep_every` of `None` means keep every version within the band (no thinning).
Tiers SHALL be applied newest-first, each tier governing the age band between the previous tier's `max_age` and its own.
The oldest tier's `max_age` bounds the policy: versions older than it fall outside all tiers and are reclaimable.
Reclamation SHALL always preserve the first and current versions when `keep_first_version`/`keep_current_version` are set, independent of tier rules — so a version selected by both a tier window and the first/current rule is retained once (the retained set is a deduplicated union).

Tier GC is activated when `VFSConfig.retention_tiers` is explicitly set (non-None, non-empty list of tier dicts); when `retention_tiers` is `None` (the default), GC falls back to the simple `max_recent_versions` path.

The MetadataStore protocol SHALL expose `iter_versions_for_gc(namespace_id, file_path) -> AsyncIterator[VersionMeta]`, a coarse enumerator returning a file's versions in deterministic order (`created_at`, then `version_number`).
Existing `list_reclaimable_versions(policy, namespace_id)` is retained for the simple rules (`max_recent_versions`, `keep_first_version`, `keep_current_version`); tier-aware reclamation consumes `iter_versions_for_gc`.

> **Deferred:** Per-namespace `Namespace.retention_policy` overrides are not yet wired
> (no `get_namespace` on `MetadataStore`); config-level policy applies uniformly to all namespaces.

#### Scenario: HourlyTierKeepsOnePerHour

- **GIVEN** a file with 60 versions written over the last 6 hours (10 per hour) and a tier `(max_age=24h, keep_every=1h)`
- **WHEN** GC runs with `keep_first_version=True`, `keep_current_version=True`, and `max_recent_versions=0`
- **THEN** exactly 7 distinct versions are retained: the 6 per-hour-window survivors (the oldest of which coincides with the first version) plus the current version (newest in its window, so retained in addition to that window's survivor)

#### Scenario: TiersCascadeNewestFirst

- **GIVEN** tiers `[(24h, None), (7d, 1h), (30d, 1d), (365d, 1wk)]` (where `None` = keep all) and versions spanning 60 days
- **WHEN** GC runs
- **THEN** versions \<24h old are all retained; versions 24h–7d old are sampled hourly; versions 7d–30d old are sampled daily; versions 30d–365d old are sampled weekly (and any version older than 365d would fall outside all tiers and be reclaimable, except first/current)

#### Scenario: FirstWithinWindowIsDeterministic

- **GIVEN** a `keep_every` window containing versions 12, 13, 14 with ascending `created_at`
- **WHEN** the tier evaluator selects the survivor for that window
- **THEN** version 12 (smallest `created_at`) is kept regardless of the order versions are enumerated

#### Scenario: ReclamationIdenticalAcrossAdapters

- **GIVEN** the same file version set and `RetentionPolicy` materialized in the SQLite, Postgres, and Mongo adapters
- **WHEN** the GC library evaluates tiers via `iter_versions_for_gc` against each adapter
- **THEN** all three adapters yield the identical set of reclaimed version IDs

### Requirement: SearchMetaReindex

The system SHALL provide a reindex operation that backfills search metadata for files written before native text search was activated or whose `params_hash` changed.
Native text search SHALL NOT trigger backfill itself.
The VFS MAY backfill opportunistically: after it reads content to verify a **bounded** straggler set (within `max_content_reads`), it MAY call `index_text` and persist the returned artifact under the same version/CAS checks used on write; a cold index (stragglers beyond the budget) is not lazily backfilled at query time — it fails loud and reindex is the remedy.
On rollback, when the new version reuses a prior version's `content_hash`, the VFS SHALL copy that version's `search_meta` rather than reindex; the copy is valid because the text record is content-addressed by `(provider_key, params_hash, content_hash)`, so the copied `external` reference resolves to the same record without aliasing a prior version's identity.

#### Scenario: BatchReindex

- **GIVEN** native text search is newly activated (or its `params_hash` changed)
- **WHEN** a principal calls reindex for a namespace and scope
- **THEN** all files in scope have their `search_meta` updated with `external` artifacts referencing content-addressed text records

#### Scenario: LazyBackfillIsBoundedAndVfsOwned

- **GIVEN** a bounded set of files (within `max_content_reads`) lacking the active provider key
- **WHEN** the VFS reads their content during straggler verification
- **THEN** native text search does not self-trigger any write, and the VFS MAY call `index_text` and persist the artifact under the write-time CAS check so later searches benefit

#### Scenario: RollbackCopiesSearchMeta

- **GIVEN** a file is rolled back to version 3, which has a native-text `external` artifact
- **WHEN** the rollback creates version N+1 with the same `content_hash`
- **THEN** `search_meta` is copied from version 3 and resolves to the same content-addressed text record — no reindex is performed
