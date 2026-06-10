# Storage — Delta Spec

> Change: `phase2-search`
> Date: 2026-05-22

## ADDED Requirements

### Requirement: NativeTextSearchStorage

The SQLite and Postgres metadata stores SHALL implement the `NativeTextSearch` capability, persisting the raw decoded text in a content-addressed `search_text_artifacts` table keyed by `(provider_key, params_hash, content_hash)` with a derived full-text index (SQLite FTS5; Postgres `tsvector` + `pg_trgm`).
These methods are introduced by this change onto the store classes built in `phase2-storage`.
`index_text` SHALL run in the same transaction as the version write on these stores.
A text artifact SHALL be reclaimed when its `content_hash` has no retained version references (the same orphan condition blob GC uses) or when its `params_hash` belongs to a retired index profile; reclamation SHALL be derived at GC time, not from an eager reference count.
MongoDB SHALL NOT expose the capability — its `native_text_search()` SHALL return `None`.
The stored text SHALL be treated as content at the same confidentiality level as blob content.

#### Scenario: ContentAddressedTextDedup

- **GIVEN** two versions (different paths) share the same `content_hash` under one index profile
- **WHEN** both are indexed
- **THEN** a single `search_text_artifacts` row holds the text, referenced by both versions' artifacts

#### Scenario: IndexTextInVersionTransaction

- **GIVEN** a write to a SQLite or Postgres store with `NativeTextSearch` active
- **WHEN** the version row is committed
- **THEN** the text artifact is committed in the same transaction (both present, or neither on rollback)

#### Scenario: TextArtifactGcFollowsContentOrphan

- **GIVEN** a `content_hash` whose last referencing version is reclaimed by GC
- **WHEN** the blob-orphan sweep runs
- **THEN** the blob and the content-addressed text artifacts for that `content_hash` are both deleted

#### Scenario: MongoHasNoNativeTextSearch

- **GIVEN** a MongoMetadataStore
- **WHEN** `native_text_search()` is called
- **THEN** it returns `None`
