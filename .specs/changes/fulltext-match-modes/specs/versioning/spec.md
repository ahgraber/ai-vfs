# Versioning — Delta Spec

> Change: `fulltext-match-modes`
> Date: 2026-06-16

## MODIFIED Requirements

### Requirement: SearchMetaReindex

> Serves: US-1
>
> Previously: the requirement governed backfill of search **metadata** (per-version
> `external` artifacts + content-addressed text records) and stated "Native text search SHALL
> NOT trigger backfill itself", framing migration around `params_hash` changes. This change
> adds a distinct, permitted operation — rebuilding a _derived index representation_ from the
> already-stored `raw_text` — and clarifies that the no-self-backfill rule governs only
> query-time content/metadata backfill, not derived-index rebuilds.

The system SHALL provide a reindex operation that backfills search metadata for files written before native text search was activated or whose `params_hash` changed.
Native text search SHALL NOT trigger **content/metadata** backfill itself: it SHALL NOT read the blob store, call `index_text`, or create `search_meta` artifacts on its own.
The VFS MAY backfill opportunistically: after it reads content to verify a **bounded** straggler set (within `max_content_reads`), it MAY call `index_text` and persist the returned artifact under the same version/CAS checks used on write; a cold index (stragglers beyond the budget) is not lazily backfilled at query time — it fails loud and reindex is the remedy.

Rebuilding a **derived index representation** from the already-stored, content-addressed
`raw_text` (see versioning `DerivedIndexRebuild`) is distinct from the above and is permitted:
it reads no blobs, creates no `search_meta` artifacts, and does not change `params_hash`, so it
is not the prohibited self-backfill.

On rollback, when the new version reuses a prior version's `content_hash`, the VFS SHALL copy
that version's `search_meta` rather than reindex; the copy is valid because the text record is
content-addressed by `(provider_key, params_hash, content_hash)`, so the copied `external`
reference resolves to the same record without aliasing a prior version's identity.

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

## ADDED Requirements

### Requirement: DerivedIndexRebuild

> Serves: US-1

When a metadata store gains a new **derived index representation** over text it already stores (e.g. the FULLTEXT word index added alongside the existing trigram index), the store SHALL build that representation from the content-addressed `search_text_artifacts.raw_text` at store initialization, without reading the blob store, without re-decoding content, and **without changing `params_hash`** (the stored text is tokenizer-independent and `params_hash` is shared across representations).
The build SHALL be an **idempotent, crash-resumable anti-join** keyed on the full identity `(provider_key, params_hash, content_hash)` scoped to the active provider profile — inserting only content absent from the representation — so a re-run after an interrupted build inserts only what is missing and never produces duplicate rows.

Because the freshness classifier (`has_text_artifacts`) observes `search_text_artifacts` only and not the derived representation, fresh-result correctness for that representation SHALL depend on the rebuild completing before serving; an interrupted rebuild SHALL be completed by the resumable anti-join on the next initialization.
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
