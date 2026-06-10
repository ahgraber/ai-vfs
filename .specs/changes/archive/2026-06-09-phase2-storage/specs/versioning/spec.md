# Versioning — Delta Spec

> Change: `phase2-storage`
> Date: 2026-05-22

## ADDED Requirements

### Requirement: TierBasedRetention

The system SHALL evaluate the time-based `RetentionPolicy.tiers` field during GC.
A `RetentionTier(max_age, keep_every)` SHALL be interpreted as: for versions within `max_age` of `now`, keep the first version observed in each consecutive `keep_every` window and reclaim the rest; "first" is the version with the smallest `created_at` in the window — a deterministic rule independent of GC ordering.
A `keep_every` of `None` means keep every version within the band (no thinning).
Tiers SHALL be applied newest-first, each tier governing the age band between the previous tier's `max_age` and its own.
The oldest tier's `max_age` bounds the policy: versions older than it fall outside all tiers and are reclaimable.
Reclamation SHALL always preserve the first and current versions when `keep_first_version`/`keep_current_version` are set, independent of tier rules — so a version selected by both a tier window and the first/current rule is retained once (the retained set is a deduplicated union).

The MetadataStore protocol SHALL gain `iter_versions_for_gc(namespace_id, file_path) -> AsyncIterator[VersionMeta]`, a coarse enumerator returning a file's versions in deterministic order (`created_at`, then `version_number`).
Existing `list_reclaimable_versions(policy, namespace_id)` is retained for the Phase 1 simple rules (`max_recent_versions`, `keep_first_version`, `keep_current_version`); tier-aware reclamation consumes `iter_versions_for_gc`.

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
