# Storage — Delta Spec

> Change: `search-realignment`
> Date: 2026-06-16 (self-healing cull folded in 2026-06-20)

## MODIFIED Requirements

### Requirement: NativeTextSearchStorage

> Serves: US-1, US-2
>
> Previously: a single "derived full-text index (SQLite FTS5; Postgres `tsvector` +
> `pg_trgm`)" was described, conflating the REGEX and FULLTEXT representations, and text-artifact
> reclamation did not require the reference check and deletion to be atomic. This change splits
> the index into two derived representations per backend, adds the non-stemming word index for
> FULLTEXT (rebuilt from `raw_text` without a `params_hash` change), and requires the GC
> reference-check→delete to be atomic so a live-referenced `content_hash`'s **text artifacts**
> are never swept (the invariant the removed query-time existence re-check incidentally guarded;
> the blob-orphan delete remains best-effort across the two stores).

The SQLite and Postgres metadata stores SHALL implement the `NativeTextSearch` capability, persisting the raw decoded text in a content-addressed `search_text_artifacts` table keyed by `(provider_key, params_hash, content_hash)`.
From that stored text each store SHALL derive **two representations**, one per search modality:

- a **trigram** representation for REGEX/substring matching (SQLite FTS5 trigram table;
  Postgres `pg_trgm` GIN index on `raw_text`), and
- a **word-token** representation for FULLTEXT matching (SQLite FTS5 `unicode61` table;
  Postgres `tsvector` computed via the `'simple'` configuration).

Both representations SHALL be **non-stemming** (language-neutral) and derived from the same `raw_text`.
The word-token representation SHALL impose no minimum token length, so sub-trigram terms (e.g. `s3`) are matchable in FULLTEXT.
Introducing or rebuilding the word representation SHALL NOT change `params_hash` (the stored `raw_text` is tokenizer-independent and `params_hash` is shared with the trigram representation); see versioning `DerivedIndexRebuild`.

`index_text` SHALL run in the same transaction as the version write on these stores and SHALL populate every derived representation for the content.
A text artifact SHALL be reclaimed when its `content_hash` has no retained version references (the same orphan condition blob GC uses) or when its `params_hash` belongs to a retired index profile; reclamation SHALL be derived at GC time, not from an eager reference count, and SHALL remove the content from all derived representations.
The orphan-reference check and the **text-artifact** deletion SHALL be atomic: a `content_hash` with any live version reference SHALL NOT have its text artifacts deleted, and a reviving write SHALL NOT interleave between the reference check and the text-artifact deletion (enforced by one metadata transaction; on best-effort stores, by re-checking references within the deletion).
This keeps a live-referenced version's stored text intact — the invariant the removed query-time existence re-check incidentally guarded — so search stays correct.
The subsequent **blob** deletion is best-effort and follows the committed transaction.
The cross-store revive race — a write reviving a `content_hash` between the metadata commit and the blob delete, which can briefly leave a live version's content blob reclaimed (a read-failure window) — is inherent to two independent stores with no shared transaction, pre-dates this change, and is accepted at PoC scale; closing it would require grace-period / generational blob GC.
Document stores SHALL NOT expose the capability — `NativeTextSearch` is a relational-exemplar feature; a document store's `native_text_search()` SHALL return `None`.
The stored text SHALL be treated as content at the same confidentiality level as blob content.

#### Scenario: TwoDerivedRepresentationsPerBackend

- **GIVEN** a SQLite or Postgres store with `NativeTextSearch` active and a text version
  written
- **WHEN** the content is indexed
- **THEN** the content is matchable both by REGEX (trigram/substring) and by FULLTEXT
  (non-stemming word tokens), and a sub-trigram FULLTEXT term such as `s3` matches the
  content that contains it

#### Scenario: ContentAddressedTextDedup

- **GIVEN** two versions (different paths) share the same `content_hash` under one index profile
- **WHEN** both are indexed
- **THEN** a single `search_text_artifacts` row holds the text, referenced by both versions' artifacts

#### Scenario: IndexTextInVersionTransaction

- **GIVEN** a write to a SQLite or Postgres store with `NativeTextSearch` active
- **WHEN** the version row is committed
- **THEN** the text artifact (and its derived representations) are committed in the same
  transaction (both present, or neither on rollback)

#### Scenario: TextArtifactGcFollowsContentOrphan

- **GIVEN** a `content_hash` whose last referencing version is reclaimed by GC
- **WHEN** the blob-orphan sweep runs
- **THEN** the blob and the content-addressed text artifacts for that `content_hash` — across
  both derived representations — are deleted

#### Scenario: LiveReferencedContentNeverSwept

- **GIVEN** a `content_hash` referenced by at least one live (non-tombstone) version
- **WHEN** the blob-orphan sweep runs
- **THEN** neither its blob nor its text artifacts are deleted — the live reference is honored
  (this scenario exercises the static case; the text-artifact no-interleave guarantee is in the
  requirement, the blob revive race is the accepted limitation noted there)

#### Scenario: MongoHasNoNativeTextSearch

- **GIVEN** a MongoMetadataStore
- **WHEN** `native_text_search()` is called
- **THEN** it returns `None`
