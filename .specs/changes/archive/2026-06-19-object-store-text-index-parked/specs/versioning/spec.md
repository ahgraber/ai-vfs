# Versioning — Delta Spec

> Change: `object-store-text-index`
> Date: 2026-06-11

## MODIFIED Requirements

### Requirement: SearchMetaReindex

> Previously: reindex backfilled `search_meta` by calling `index_text` and persisting the artifact under the write-time CAS check, assuming a synchronous capability whose `index_text` commits in the version write transaction; pre-activation files and `params_hash` changes were the only sources of staleness.

The system SHALL provide a reindex operation that backfills search metadata for files written before native text search was activated or whose `params_hash` changed.
Native text search SHALL NOT trigger backfill itself.
The VFS MAY backfill opportunistically: after it reads content to verify a **bounded** straggler set (within `max_content_reads`), it MAY call `index_text` and persist the returned artifact under the same version/CAS checks used on write; a cold index (stragglers beyond the budget) is not lazily backfilled at query time — it fails loud and reindex is the remedy.

When the active native text search uses the **deferred** discipline (the object-store index), indexing does not run in the version write transaction, so **every** freshly written version is initially an unindexed straggler until materialized.
For such a capability, reindex SHALL be the materialize operation: it SHALL seal the in-scope staged content into immutable segments and publish them in the order **write segment → CAS-update the manifest → backfill each version's `external` artifact** under the write-time CAS check.
Because the index is manually materialized, an operator SHALL run reindex after bulk writes; until then, fulltext over an unsealed set larger than `max_content_reads` fails loud (reindex-required) rather than returning partial results.

On rollback, when the new version reuses a prior version's `content_hash`, the VFS SHALL copy that version's `search_meta` rather than reindex; the copy is valid because the text record is content-addressed by `(provider_key, params_hash, content_hash)`, so the copied `external` reference (a logical identity, not a physical location) resolves to the same record without aliasing a prior version's identity.

#### Scenario: BatchReindex

- **GIVEN** native text search is newly activated (or its `params_hash` changed)
- **WHEN** a principal calls reindex for a namespace and scope
- **THEN** all files in scope have their `search_meta` updated with `external` artifacts referencing content-addressed text records

#### Scenario: DeferredReindexSealsThenCasPublishesThenBackfills

- **GIVEN** an active object-store (deferred) index and in-scope versions whose content is staged but not sealed
- **WHEN** a principal calls reindex for the namespace and scope
- **THEN** the in-scope content is sealed into immutable segments, the partition manifest is CAS-updated to include them, and only then is each version's `external` artifact backfilled under the write-time CAS check

#### Scenario: FreshWriteIsStragglerUntilMaterialized

- **GIVEN** an active object-store (deferred) index
- **WHEN** a new version is written
- **THEN** indexing does not run inside the version write transaction, the version carries no fresh artifact for the object-store provider, and it is a bounded straggler (verified via the guarded reader) until a reindex/materialize pass backfills its artifact

#### Scenario: LazyBackfillIsBoundedAndVfsOwned

- **GIVEN** a bounded set of files (within `max_content_reads`) lacking the active provider key
- **WHEN** the VFS reads their content during straggler verification
- **THEN** native text search does not self-trigger any write, and the VFS MAY call `index_text` and persist the artifact under the write-time CAS check so later searches benefit

#### Scenario: RollbackCopiesSearchMeta

- **GIVEN** a file is rolled back to version 3, which has a native-text `external` artifact
- **WHEN** the rollback creates version N+1 with the same `content_hash`
- **THEN** `search_meta` is copied from version 3 and its logical `external` reference resolves to the same content-addressed text record — no reindex is performed
