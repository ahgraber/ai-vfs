# Versioning — Delta Spec

> Change: `phase2-adapters`
> Date: 2026-04-04
> Design reference: `.specs/ai-vfs-bloom-provider-design.md` (Section 6)

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
