# Versioning — Delta Spec

> Change: `phase2-search`
> Date: 2026-05-22

## MODIFIED Requirements

### Requirement: SearchMetaReindex

> Previously: only batch reindex was specified; lazy backfill and rollback copy are added, and backfill is explicitly VFS-owned and bounded.

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
