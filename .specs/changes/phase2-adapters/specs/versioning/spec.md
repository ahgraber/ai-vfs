# Versioning — Delta Spec

> Change: `phase2-adapters`
> Date: 2026-04-04
> Design reference: `.specs/ai-vfs-bloom-provider-design.md` (Section 6)

## ADDED Requirements

### Requirement: TierBasedRetention

The system SHALL evaluate the time-based `RetentionPolicy.tiers` field during GC.
Tier evaluation logic SHALL live in the library (`GarbageCollector`), not in any metadata store adapter — the canonical implementation runs once for every store backend.
The metadata store protocol SHALL gain `iter_versions_for_gc(namespace_id, file_path) -> AsyncIterator[VersionMeta]`, a coarse enumerator that returns versions in deterministic order so the library can apply tier rules client-side.
Existing `list_reclaimable_versions(policy, namespace_id)` is retained for Phase 1's simple rules (`max_recent_versions`, `keep_first_version`, `keep_current_version`); tier-aware reclamation invokes the library evaluator via `iter_versions_for_gc`.

A `RetentionTier(max_age, keep_every)` SHALL be interpreted as: within `max_age` of `now`, keep the first version observed in each consecutive `keep_every` window; reclaim the rest.
"First" is defined as the version with the smallest `created_at` within the window — a stable, deterministic selection rule independent of GC ordering.

#### Scenario: HourlyTierKeepsOnePerHour

- **GIVEN** a file with 60 versions written over the last 6 hours (10 per hour) and a tier `(max_age=24h, keep_every=1h)`
- **WHEN** GC runs with `keep_first_version=True`, `keep_current_version=True`, and `max_recent_versions=0`
- **THEN** exactly 6 versions are retained (one per hour window), plus the first version and the current version

#### Scenario: TiersCascadeNewestFirst

- **GIVEN** tiers `[(24h, all), (7d, 1h), (30d, 1d), (>30d, 1wk)]` and versions spanning 60 days
- **WHEN** GC runs
- **THEN** versions \<24h old are all retained; versions 24h–7d old are sampled hourly; versions 7d–30d old are sampled daily; versions older than 30d are sampled weekly

#### Scenario: StoreAdapterIsAgnostic

- **GIVEN** any metadata store adapter (SQLite, Postgres, Mongo)
- **WHEN** the GC library evaluates tiers
- **THEN** the adapter implements only `iter_versions_for_gc` — no tier semantics, no time-window math; the library produces identical reclamation decisions across adapters

## MODIFIED Requirements

### Requirement: SearchMetaReindex

The system SHALL provide a reindex operation that backfills search metadata
for files written before a search provider was activated.
(Previously: only batch reindex was demonstrated; lazy backfill on search miss
is added for bloom provider integration.)

The provider SHALL NOT trigger backfill directly — it returns unindexed files as candidates (conservative).
The VFS decides whether to backfill opportunistically after content is read for verification.

#### Scenario: LazyBackfill

- **GIVEN** a file exists without a `"bloom"` key in search_meta
- **WHEN** the VFS reads the file's content during search verification
- **THEN** the VFS optionally calls `provider.index()` and updates search_meta,
  so subsequent searches benefit from the index

#### Scenario: BatchReindex

- **GIVEN** a bloom search provider is newly activated (or normalizer config changes)
- **WHEN** a principal calls reindex for a namespace and scope
- **THEN** all files in scope have their search_meta updated with bloom artifacts

#### Scenario: RollbackCopiesSearchMeta

- **GIVEN** a file is rolled back to version 3, which has bloom search_meta
- **WHEN** the rollback creates version N+1 with the same content_hash
- **THEN** search_meta is copied from version 3 — no reindex needed
