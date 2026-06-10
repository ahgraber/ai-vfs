# Storage — Delta Spec

> Change: `phase2-storage`
> Date: 2026-05-22

## MODIFIED Requirements

### Requirement: MetadataStoreProtocol

> Previously: only the SQLite adapter was specified.

The system SHALL define a MetadataStore protocol with methods for file CRUD, version CRUD, permission checks, audit event persistence, name resolution, search metadata updates, and GC queries.
All adapters SHALL implement this protocol.
The protocol SHALL be satisfied by the SQLite, Postgres, and Mongo adapters, each round-tripping files and versions identically and persisting the extensible `search_meta` and `detail` fields as backend-native structures (JSON for SQLite, JSONB for Postgres, subdocuments for Mongo).

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

> Previously: only the local FS adapter was specified.

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

### Requirement: BlobPrefixDirectoryStructure

> Previously: only the local FS store was specified.

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

> Previously: auto mode disabled the cache for `file:///` and deferred remote-scheme auto-enable to Phase 2.

The system SHALL provide an optional diskcache-backed caching layer for blob stores.
Cache is keyed by content hash.
When `blob_cache_enabled` is explicitly set, that value wraps (True) or bypasses (False) any blob store.
When `None` (auto), the cache wraps remote stores (`s3://`) and is disabled for local FS (`file:///`).

#### Scenario: RemoteAutoEnable

- **GIVEN** blob_store_uri="s3://my-bucket/aifs" and `blob_cache_enabled` unset (auto)
- **WHEN** the VFS is initialized
- **THEN** the S3 blob store is wrapped in the diskcache layer

#### Scenario: LocalAutoDisable

- **GIVEN** blob_store_uri="file:///./aifs_blobs/" and `blob_cache_enabled` unset (auto)
- **WHEN** the VFS is initialized
- **THEN** the local FS blob store is not wrapped

### Requirement: MetadataCASSemantics

> Previously: only SQL adapter CAS behavior was specified.

The MetadataStore SHALL implement compare-and-swap semantics for version mutations.
SQL adapters SHALL use `WHERE version_number = ?` returning zero rows on mismatch.
NoSQL adapters SHALL use atomic find-and-update with version matching.

#### Scenario: CASConflictDetected

- **GIVEN** a file at version 5
- **WHEN** put_version is called with expected_version=3
- **THEN** a ConflictError is raised

#### Scenario: MongoCASConflict

- **GIVEN** a file at version 5 in MongoMetadataStore
- **WHEN** put_version is called with expected_version=3
- **THEN** a ConflictError is raised via `find_one_and_update` with version match returning no document

### Requirement: MetadataTransactions

> Previously: only SQLite transaction behavior and the no-op fallback were specified.

The MetadataStore protocol SHALL include an optional `transaction()` async context manager for atomic multi-step operations.
SQLite and Postgres implement this via `BEGIN`/`COMMIT`/`ROLLBACK`.
MongoDB provides true atomicity only on replica-set deployments; on standalone MongoDB `transaction()` is a documented best-effort no-op.
Callers performing multi-document mutations SHALL NOT rely on `transaction()` rollback for non-destructiveness on stores where it is best-effort; they SHALL order writes so partial failure is non-destructive (see file-operations `MoveFile`).

#### Scenario: TransactionRollbackOnError

- **GIVEN** a transaction is active on a SQL store
- **WHEN** an exception occurs during the transaction
- **THEN** all operations within the transaction are rolled back

### Requirement: URIBasedStoreResolution

> Previously: only the `sqlite:///` and `file:///` schemes were specified.

The system SHALL resolve storage adapter implementations from URI schemes at VFS construction time, including `sqlite:///`, `postgresql://`, `mongodb://` (metadata) and `file:///`, `s3://` (blob).
Importing an adapter whose optional dependency is not installed SHALL raise a clear, actionable error naming the missing extra.

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
