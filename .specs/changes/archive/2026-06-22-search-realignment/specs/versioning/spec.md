# Versioning — Delta Spec

> Change: `search-realignment`
> Date: 2026-06-16 (self-healing cull folded in 2026-06-20)

## MODIFIED Requirements

### Requirement: SearchMetaReindex

> Serves: US-1, US-2, US-3
>
> Previously: the requirement governed backfill of search **metadata** and permitted the VFS to
> lazily backfill a **bounded** straggler set during search. This change removes the lazy-backfill
> `MAY` (the search path performs no writes; `reindex` is the sole remedy and a straggler fails
> loud) and extends `search_meta` propagation from rollback alone to all derived versions —
> rollback, copy, and move destinations. The baseline `LazyBackfillIsBoundedAndVfsOwned`
> scenario is removed (the lazy-backfill `MAY` is gone) and replaced by
> `SearchPerformsNoLazyBackfill` below.

<!-- modified-removes: LazyBackfillIsBoundedAndVfsOwned -->

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

## ADDED Requirements

### Requirement: DerivedIndexRebuild

> Serves: US-1

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
