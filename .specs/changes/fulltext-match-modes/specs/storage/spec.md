# Storage — Delta Spec

> Change: `fulltext-match-modes`
> Date: 2026-06-16

## MODIFIED Requirements

### Requirement: NativeTextSearchStorage

> Serves: US-1
>
> Previously: a single "derived full-text index (SQLite FTS5; Postgres `tsvector` +
> `pg_trgm`)" was described, conflating the REGEX and FULLTEXT representations. This change
> splits them into two derived representations per backend, adds the non-stemming word index
> for FULLTEXT, and specifies that the word index is (re)built from the stored `raw_text`
> without a `params_hash` change.

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

#### Scenario: MongoHasNoNativeTextSearch

- **GIVEN** a MongoMetadataStore
- **WHEN** `native_text_search()` is called
- **THEN** it returns `None`
