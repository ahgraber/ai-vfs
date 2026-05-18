# Storage — Spec

## Requirements

### Requirement: MetadataStoreProtocol

The system SHALL define a MetadataStore protocol with methods for file CRUD, version CRUD, permission checks, audit event persistence, name resolution, search metadata updates, and GC queries.
All adapters SHALL implement this protocol.

#### Scenario: SQLiteAdapter

- **GIVEN** metadata_store_uri="sqlite:///./aifs.db"
- **WHEN** the VFS is initialized
- **THEN** a SQLiteMetadataStore is instantiated and tables are created

### Requirement: BlobStoreProtocol

The system SHALL define a BlobStore protocol with put, get, delete, exists
methods for whole-object operations, and put_stream, get_stream methods
for streaming (initially raising NotImplementedError).

#### Scenario: LocalFSAdapter

- **GIVEN** blob_store_uri="file:///./aifs_blobs/"
- **WHEN** the VFS is initialized
- **THEN** a LocalBlobStore is instantiated with the specified base path

### Requirement: BlobIdempotentPut

The system SHALL make blob put operations idempotent.
If a blob with the given content hash already exists, put SHALL be a no-op.

#### Scenario: DuplicatePutNoop

- **GIVEN** a blob with hash "abc123" already exists
- **WHEN** put("abc123", data) is called again
- **THEN** no error occurs and the existing blob is unchanged

### Requirement: BlobPrefixDirectoryStructure

The local filesystem blob store SHALL store blobs in a prefix-based
directory structure ({hash[0:2]}/{hash[2:4]}/{hash}) to avoid large
flat directories.

#### Scenario: BlobPathStructure

- **GIVEN** a blob with hash "abcdef1234..."
- **WHEN** the blob is stored
- **THEN** it is written to {base_path}/ab/cd/abcdef1234...

### Requirement: BlobCaching

The system SHALL provide an optional diskcache-backed caching layer for blob stores.
Cache is keyed by content hash.
Cache SHALL be disabled by default for local FS.
When `blob_cache_enabled` is explicitly set to `True`, the cache wraps any blob store.
When `None` (auto), cache is disabled for `file:///` URIs; Phase 2 will auto-enable for remote schemes.

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
SQLite implements this via `BEGIN`/`COMMIT`/`ROLLBACK`.
Stores that do not support transactions MAY implement `transaction()` as a no-op context manager with a documentation note.

#### Scenario: TransactionRollbackOnError

- **GIVEN** a transaction is active
- **WHEN** an exception occurs during the transaction
- **THEN** all operations within the transaction are rolled back

### Requirement: MetadataCASSemantics

The MetadataStore SHALL implement compare-and-swap semantics for version mutations.
SQL adapters SHALL use `WHERE version_number = ?` returning zero rows on mismatch.

#### Scenario: CASConflictDetected

- **GIVEN** a file at version 5
- **WHEN** put_version is called with expected_version=3
- **THEN** a ConflictError is raised

### Requirement: URIBasedStoreResolution

The system SHALL resolve storage adapter implementations from URI schemes at VFS construction time.

#### Scenario: URIResolution

- **GIVEN** metadata_store_uri starts with "sqlite:///"
- **WHEN** the VFS initializes
- **THEN** SQLiteMetadataStore is used

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
