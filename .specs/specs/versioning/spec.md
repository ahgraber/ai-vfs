# Versioning — Spec

> **Why (trust thesis):** immutable history and rollback are the _reversibility_ facet of `NORTH-STAR.md` bet #2 (trust) — an agent's bad write is always recoverable. The rationale lives in the north star; this spec is the contract.

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

> **Optional capability (advanced, opt-in).** Tiered retention is not part of the core
> contract: it is inert unless `VFSConfig.retention_tiers` is explicitly set, and the
> default policy uses only `max_recent_versions`. A backend that implements only the simple
> retention path is conformant; tier evaluation is additive.

The MetadataStore protocol SHALL expose `iter_versions_for_gc(namespace_id, file_path) -> AsyncIterator[VersionMeta]`, a coarse enumerator returning a file's versions in deterministic order (`created_at`, then `version_number`).
Existing `list_reclaimable_versions(policy, namespace_id)` is retained for the simple rules (`max_recent_versions`, `keep_first_version`, `keep_current_version`); tier-aware reclamation consumes `iter_versions_for_gc`.

> **Cross-adapter conformance (conditional).** The pure tier evaluator is the single source
> of truth; an adapter is conformant when, given the same version set, it yields the
> evaluator's reclaimed-ID set. This is proven continuously on SQLite; the Postgres and
> document-store legs are conformance-tested when that infrastructure is configured.
> Conformance depends on every adapter enumerating versions in one deterministic order
> (`created_at`, then `version_number`); a document store that persists `created_at` as
> ISO-8601 text and sorts lexically satisfies this only for fixed-offset UTC timestamps — a
> required storage invariant.
>
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

- **GIVEN** the same file version set and `RetentionPolicy` materialized in each configured adapter (SQLite always; Postgres and the document store when their infrastructure is available)
- **WHEN** the GC library evaluates tiers via `iter_versions_for_gc` against each adapter
- **THEN** each configured adapter yields the reclaimed version-ID set computed by the pure tier evaluator

### Requirement: SearchMetaReindex

The system SHALL provide a reindex operation that backfills search metadata for files written before native text search was activated or whose `params_hash` changed.
Native text search SHALL NOT trigger **content/metadata** backfill itself: it SHALL NOT read the blob store, call `index_text`, or create `search_meta` artifacts on its own.
The search path SHALL NOT write — there is no query-time lazy backfill; a stale or cold index fails loud (`ReindexRequiredError`) and `reindex` is the remedy.

Rebuilding a **derived index representation** from the already-stored, content-addressed
`raw_text` (see versioning `DerivedIndexRebuild`) is distinct from the above and is permitted:
it reads no blobs, creates no `search_meta` artifacts, and does not change `params_hash`, so it
is not the prohibited self-backfill.

When an operation creates a new version that reuses an existing version's `content_hash` — rollback, copy, or a move destination — the VFS SHALL propagate the source version's `search_meta` to the new version rather than reindex.
The copy is valid because the text record is content-addressed by `(provider_key, params_hash, content_hash)`, so the propagated `external` reference resolves to the same record without aliasing a prior version's identity; the derived version is therefore immediately fresh — searchable with no blob read and without triggering `reindex`.
A derived version that omitted `search_meta` would be a perpetual straggler (its content is indexed, but the manifest pointer is missing), failing every search over its scope until reindex — so propagation is what keeps `copy`/`move` on the authoritative fresh path.

#### Scenario: BatchReindex

- **GIVEN** native text search is newly activated (or its `params_hash` changed)
- **WHEN** a principal calls reindex for a namespace and scope
- **THEN** all files in scope have their `search_meta` updated with `external` artifacts referencing content-addressed text records

#### Scenario: RollbackCopiesSearchMeta

- **GIVEN** a file is rolled back to version 3, which has a native-text `external` artifact
- **WHEN** the rollback creates version N+1 with the same `content_hash`
- **THEN** `search_meta` is copied from version 3 and resolves to the same content-addressed text record — no reindex is performed

#### Scenario: CopyPropagatesSearchMeta

- **GIVEN** a fresh-indexed file `/a.py` with a native-text `external` artifact
- **WHEN** it is copied to `/b.py` and a search matching its content runs
- **THEN** `/b.py` carries the source `search_meta`, so both `/a.py` and `/b.py` are returned with no blob reads and no `ReindexRequiredError`

#### Scenario: MoveDestinationPropagatesSearchMeta

- **GIVEN** a fresh-indexed file `/old.py` with a native-text `external` artifact
- **WHEN** it is moved to `/new.py` and a search matching its content runs
- **THEN** `/new.py` carries the source `search_meta` and is returned with no blob reads and no `ReindexRequiredError`; `/old.py` (now a tombstone) is absent

#### Scenario: SearchPerformsNoLazyBackfill

- **GIVEN** an in-scope version that is a straggler (missing or identity-drifted artifact)
- **WHEN** a search runs and fails loud, and the operator then runs `reindex`
- **THEN** the index is repaired by `reindex` alone; the failed search wrote nothing, and a second search before reindex still fails loud

### Requirement: DerivedIndexRebuild

When a metadata store gains a new **derived index representation** over text it already stores (e.g. the FULLTEXT word index added alongside the existing trigram index), the store SHALL build that representation from the content-addressed `search_text_artifacts.raw_text` at store initialization, without reading the blob store, without re-decoding content, and **without changing `params_hash`** (the stored text is tokenizer-independent and `params_hash` is shared across representations).
The build SHALL be an **idempotent, crash-resumable anti-join** keyed on the full identity `(provider_key, params_hash, content_hash)` scoped to the active provider profile — inserting only content absent from the representation — so a re-run after an interrupted build inserts only what is missing and never produces duplicate rows.

Because the freshness classifier reasons over the per-version artifact manifest (and the presence of a `raw_text` record), not the derived representation, fresh-result correctness for that representation SHALL depend on the rebuild completing before serving; an interrupted rebuild SHALL be completed by the resumable anti-join on the next initialization.
The retrieval-behavior change MAY be signaled by bumping the informational `SearchArtifact.provider_version` (which does not participate in artifact usability); the rebuild, not the version field, is what makes the migration correct.

#### Scenario: WordIndexBackfilledFromRawTextWithoutBlobReads

- **GIVEN** existing `search_text_artifacts` rows (`raw_text` present) indexed before the
  word representation existed
- **WHEN** the SQLite store initializes and a FULLTEXT search over that content runs
- **THEN** the word index is populated from `raw_text`, the search returns correct results,
  and the guarded reader performs zero blob reads (records remain fresh because `params_hash`
  is unchanged)

#### Scenario: DerivedIndexRebuildIsIdempotentAndResumable

- **GIVEN** a store whose word index is partially built (some content present, some missing)
- **WHEN** initialization runs the anti-join rebuild again
- **THEN** only the missing `(provider_key, params_hash, content_hash)` rows are inserted, no
  duplicate rows are created, and a FULLTEXT search returns no duplicate occurrences
