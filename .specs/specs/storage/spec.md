# Storage — Spec

## Requirements

### Requirement: MetadataStoreProtocol

The system SHALL define a MetadataStore protocol with methods for file CRUD, version CRUD, permission checks, audit event persistence, name resolution, search metadata updates, and GC queries.
All adapters SHALL implement this protocol.
The protocol spans two backend families — **relational** (SQLite, Postgres, and any other RDBMS) and **document** (MongoDB-wire stores, including Azure Cosmos DB for MongoDB) — and is defined at the **weakest common denominator** of the two, so any backend in either family is substitutable behind it (see `MetadataTransactions`, `MetadataCASSemantics`).
The shipped exemplar adapters are SQLite, Postgres, and MongoDB; each round-trips files and versions identically and persists the extensible `search_meta` and `detail` fields as backend-native structures (JSON for SQLite, JSONB for Postgres, subdocuments for document stores).

#### Scenario: SQLiteAdapter

- **GIVEN** metadata_store_uri="sqlite:///./aifs.db"
- **WHEN** the VFS is initialized
- **THEN** a SQLiteMetadataStore is instantiated and tables are created

#### Scenario: PostgresAdapterRoundTrip

- **GIVEN** a PostgresMetadataStore initialized against metadata_store_uri="postgresql://localhost/aifs"
- **WHEN** a file and a version carrying non-empty `search_meta` and `detail` are written, then read back
- **THEN** the returned FileMeta and VersionMeta equal what was written, including the `search_meta` and `detail` contents stored as JSONB

#### Scenario: MongoAdapterRoundTrip

- **GIVEN** a MongoMetadataStore initialized against metadata_store_uri="mongodb://localhost/aifs"
- **WHEN** a file and a version carrying non-empty `search_meta` and `detail` are written, then read back
- **THEN** the returned FileMeta and VersionMeta equal what was written, including the `search_meta` and `detail` contents stored as native subdocuments

### Requirement: BlobStoreProtocol

The system SHALL define a BlobStore protocol with put, get, delete, exists methods for whole-object operations, and put_stream, get_stream methods for streaming (initially raising NotImplementedError).
The protocol SHALL be satisfied by the local FS and S3 adapters, each storing and returning blob bytes verbatim under content-hash keys.

#### Scenario: LocalFSAdapter

- **GIVEN** blob_store_uri="file:///./aifs_blobs/"
- **WHEN** the VFS is initialized
- **THEN** a LocalBlobStore is instantiated with the specified base path

#### Scenario: S3AdapterRoundTrip

- **GIVEN** an S3BlobStore initialized against blob_store_uri="s3://my-bucket/aifs"
- **WHEN** `put(content_hash, data)` is called and then `get(content_hash)`
- **THEN** the returned bytes equal `data`, and a second `put` of the same hash is a no-op

### Requirement: BlobIdempotentPut

The system SHALL make blob put operations idempotent.
If a blob with the given content hash already exists, put SHALL be a no-op.

#### Scenario: DuplicatePutNoop

- **GIVEN** a blob with hash "abc123" already exists
- **WHEN** put("abc123", data) is called again
- **THEN** no error occurs and the existing blob is unchanged

### Requirement: BlobPrefixDirectoryStructure

Content-addressed blob stores SHALL key blobs under a sharded `{hash[0:2]}/{hash[2:4]}/{hash}` layout.
The local FS store applies this as a directory path; the S3 store applies it as an object key prefix.

#### Scenario: BlobPathStructure

- **GIVEN** a blob with hash "abcdef1234..."
- **WHEN** the blob is stored in the local FS store
- **THEN** it is written to {base_path}/ab/cd/abcdef1234...

#### Scenario: S3KeyStructure

- **GIVEN** a blob with hash "abcdef1234..." and blob_store_uri="s3://my-bucket/aifs"
- **WHEN** the blob is stored in the S3 store
- **THEN** the object key is aifs/ab/cd/abcdef1234...

### Requirement: BlobCaching

The system SHALL provide an optional diskcache-backed caching layer for blob stores.
Cache is keyed by content hash.
When `blob_cache_enabled` is explicitly set, that value wraps (True) or bypasses (False) any blob store.
When `None` (auto), the cache wraps remote stores (`s3://`) and is disabled for local FS (`file:///`).

#### Scenario: CacheHit

- **GIVEN** a blob was previously read and cached
- **WHEN** the same blob is read again
- **THEN** the blob is served from cache without accessing the inner store

#### Scenario: CacheWriteThrough

- **GIVEN** caching is enabled
- **WHEN** a blob is written via the cached store
- **THEN** the blob is stored in both the cache and the inner store

#### Scenario: CacheEviction

- **GIVEN** the cache has reached its max_size_mb limit
- **WHEN** a new blob is cached
- **THEN** the least recently used blob is evicted

#### Scenario: RemoteAutoEnable

- **GIVEN** blob_store_uri="s3://my-bucket/aifs" and `blob_cache_enabled` unset (auto)
- **WHEN** the VFS is initialized
- **THEN** the S3 blob store is wrapped in the diskcache layer

#### Scenario: LocalAutoDisable

- **GIVEN** blob_store_uri="file:///./aifs_blobs/" and `blob_cache_enabled` unset (auto)
- **WHEN** the VFS is initialized
- **THEN** the local FS blob store is not wrapped

### Requirement: StreamingProvisions

The BlobStore protocol SHALL include put_stream and get_stream methods for large file handling.
Initial adapters MAY raise NotImplementedError for these methods.

#### Scenario: StreamingNotYetImplemented

- **GIVEN** a LocalBlobStore
- **WHEN** put_stream is called
- **THEN** NotImplementedError is raised

### Requirement: BlobEnumeration

The BlobStore protocol SHALL include a method to enumerate all stored content hashes,
enabling garbage collection to identify orphaned blobs with zero version references.

#### Scenario: EnumerateAllBlobs

- **GIVEN** blobs with hashes "aaa...", "bbb...", "ccc..." exist in the store
- **WHEN** the blob store is enumerated
- **THEN** all three hashes are returned

### Requirement: MetadataTransactions

The MetadataStore protocol SHALL include an optional `transaction()` async context manager for atomic multi-step operations.

**Contract floor (weakest common denominator).**
Multi-document atomicity is NOT part of the protocol contract — it is a relational-family bonus, not a document-family guarantee.
Relational exemplars (SQLite, Postgres) implement true transactions via `BEGIN`/`COMMIT`/`ROLLBACK`.
Document stores provide it only conditionally — MongoDB only on replica-set deployments, and Azure Cosmos DB for MongoDB only within tier/version-specific limits — so on a standalone MongoDB (or any document store lacking it) `transaction()` is a documented best-effort no-op.
Because the floor excludes multi-document atomicity, callers performing multi-document mutations SHALL NOT rely on `transaction()` rollback for non-destructiveness; they SHALL order writes so partial failure is non-destructive (see file-operations `MoveFile`).

> **Pre-implementation gate (document family):** before implementing or certifying a document-store adapter, verify the target deployment's actual transaction support, Mongo wire-protocol version, and indexing/throughput behavior (e.g. Cosmos RU limits and required field indexes) against current vendor documentation — the Mongo-wire compatibility surface differs across MongoDB replica sets and Cosmos tiers.

#### Scenario: TransactionRollbackOnError

- **GIVEN** a transaction is active on a SQL store
- **WHEN** an exception occurs during the transaction
- **THEN** all operations within the transaction are rolled back

### Requirement: MetadataCASSemantics

The MetadataStore SHALL implement compare-and-swap semantics for version mutations.
Relational adapters SHALL use `WHERE version_number = ?` returning zero rows on mismatch.
Document adapters SHALL use atomic find-and-update with version matching.

> **In the contract floor:** unlike multi-document transactions, **single-document CAS is part of the floor** — every adapter in both families provides it. It is the coordination primitive the VFS relies on (CAS writes plus destination-before-source ordering) precisely because multi-document atomicity is not guaranteed.

When two concurrent writers both attempt to insert the same `version_number` without an `expected_version` (no-CAS write), the store SHALL translate the resulting unique-constraint violation (`IntegrityError` for SQL, `DuplicateKeyError` for MongoDB) into a `VersionCollisionError` — distinct from `ConflictError`.
The VFS layer retries on `VersionCollisionError`; `ConflictError` from a CAS mismatch continues to propagate un-retried.

#### Scenario: CASConflictDetected

- **GIVEN** a file at version 5
- **WHEN** put_version is called with expected_version=3
- **THEN** a ConflictError is raised

#### Scenario: MongoCASConflict

- **GIVEN** a file at version 5 in MongoMetadataStore
- **WHEN** put_version is called with expected_version=3
- **THEN** a ConflictError is raised via `find_one_and_update` with version match returning no document

#### Scenario: NoCASVersionCollision

- **GIVEN** a file at version N
- **WHEN** two concurrent writers both call put_version with version_number=N+1 and no expected_version
- **THEN** the losing writer receives VersionCollisionError (not IntegrityError or DuplicateKeyError)

### Requirement: URIBasedStoreResolution

The system SHALL resolve storage adapter implementations from URI schemes at VFS construction time, including `sqlite:///`, `postgresql://`, `mongodb://` (metadata) and `file:///`, `s3://` (blob).
The `mongodb://` scheme covers any Mongo-wire-compatible document store, including Azure Cosmos DB for MongoDB.
Importing an adapter whose optional dependency is not installed SHALL raise a clear, actionable error naming the missing extra.

#### Scenario: URIResolution

- **GIVEN** metadata_store_uri starts with "sqlite:///"
- **WHEN** the VFS initializes
- **THEN** SQLiteMetadataStore is used

#### Scenario: PostgresURIResolution

- **GIVEN** metadata_store_uri starts with "postgresql://"
- **WHEN** the VFS initializes
- **THEN** PostgresMetadataStore is used

#### Scenario: MongoURIResolution

- **GIVEN** metadata_store_uri starts with "mongodb://"
- **WHEN** the VFS initializes
- **THEN** MongoMetadataStore is used

#### Scenario: S3URIResolution

- **GIVEN** blob_store_uri starts with "s3://"
- **WHEN** the VFS initializes
- **THEN** S3BlobStore is used

#### Scenario: MissingExtraRaises

- **GIVEN** a `postgresql://` URI but the `asyncpg` extra is not installed
- **WHEN** the VFS initializes
- **THEN** an error is raised naming the missing optional dependency

### Requirement: PydanticSettingsConfig

The system SHALL use pydantic-settings for configuration, with environment
variable support (AIFS\_ prefix) and sensible local defaults.

#### Scenario: EnvironmentOverride

- **GIVEN** AIFS_BLOB_STORE_URI is set to "file:///tmp/custom_blobs/"
- **WHEN** VFSConfig is constructed without arguments
- **THEN** the custom blob path is used

#### Scenario: SensibleDefaults

- **GIVEN** no environment variables or arguments
- **WHEN** VFSConfig is constructed
- **THEN** metadata uses SQLite, blobs use local FS, OTel is enabled, audit is enabled

### Requirement: ProcessIdentification

The system SHALL set a descriptive process title using setproctitle when running as a service or background GC process.

#### Scenario: GCProcessTitle

- **GIVEN** ai-vfs GC is running as a background process
- **WHEN** the process list is inspected
- **THEN** the process title contains "ai-vfs:"

### Requirement: PrefixQueryLiteralMatching

The system SHALL treat path-prefix arguments to storage queries as **literal strings**, not as SQL LIKE patterns or regex expressions.
Any `%`, `_`, or `\` characters in a path prefix must be escaped before use in a SQL `LIKE` clause (using an `ESCAPE` clause), and before use in a MongoDB `$regex` query (using `re.escape`).

A file at path `/my_dir/report.txt` SHALL be returned when listing the prefix
`/my_dir/`, and SHALL NOT be returned when listing `/myXdir/` (where `X` is any single
character), even though the SQL LIKE pattern `/my_dir/%` would match `/myXdir/` if `_`
were treated as a wildcard.

#### Scenario: UnderscoreInPrefixMatchesLiterally

- **GIVEN** a file exists at `/my_dir/report.txt`
- **WHEN** the prefix `/my_dir/` is used to list files
- **THEN** `/my_dir/report.txt` is returned and `/myXdir/report.txt` is NOT returned

#### Scenario: PercentInPrefixMatchesLiterally

- **GIVEN** a file exists at `/data%2F/file.txt`
- **WHEN** the prefix `/data%2F/` is used to list files
- **THEN** `/data%2F/file.txt` is returned and only files under that exact prefix are returned

### Requirement: NativeTextSearchStorage

The SQLite and Postgres metadata stores SHALL implement the `NativeTextSearch` capability, persisting the raw decoded text in a content-addressed `search_text_artifacts` table keyed by `(provider_key, params_hash, content_hash)` with a derived full-text index (SQLite FTS5; Postgres `tsvector` + `pg_trgm`).
`index_text` SHALL run in the same transaction as the version write on these stores.
A text artifact SHALL be reclaimed when its `content_hash` has no retained version references (the same orphan condition blob GC uses) or when its `params_hash` belongs to a retired index profile; reclamation SHALL be derived at GC time, not from an eager reference count.
Document stores SHALL NOT expose the capability — `NativeTextSearch` is a relational-exemplar feature built on FTS5 / `tsvector`+`pg_trgm`, so a document store's `native_text_search()` SHALL return `None`.
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
