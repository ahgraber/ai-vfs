# Storage Specification

> Generated from design document analysis on 2026-04-04
> Source files: docs/specs/2026-04-04-ai-vfs-design.md (Sections 3, 9, 11, 12)

## Purpose

Pluggable storage protocols for metadata and blob content.
MetadataStore handles files, versions, permissions, audit events, names, and search metadata.
BlobStore handles immutable content-addressed blobs.
Adapters are resolved from URI schemes at construction.
A caching layer wraps remote blob stores.

## Requirements

### Requirement: MetadataStoreProtocol

The system SHALL define a MetadataStore protocol with methods for file CRUD, version CRUD, permission checks, audit event persistence, name resolution, search metadata updates, and GC queries.
All adapters SHALL implement this protocol.

#### Scenario: SQLiteAdapter

- **GIVEN** metadata_store_uri="sqlite:///./aifs.db"
- **WHEN** the VFS is initialized
- **THEN** a SQLiteMetadataStore is instantiated and tables are created

#### Scenario: PostgresAdapter

- **GIVEN** metadata_store_uri="postgresql://localhost/aifs"
- **WHEN** the VFS is initialized
- **THEN** a PostgresMetadataStore is instantiated

#### Scenario: MongoAdapter

- **GIVEN** metadata_store_uri="mongodb://localhost/aifs"
- **WHEN** the VFS is initialized
- **THEN** a MongoMetadataStore is instantiated

### Requirement: BlobStoreProtocol

The system SHALL define a BlobStore protocol with put, get, delete, exists
methods for whole-object operations, and put_stream, get_stream methods
for streaming (initially raising NotImplementedError).

#### Scenario: LocalFSAdapter

- **GIVEN** blob_store_uri="file:///./aifs_blobs/"
- **WHEN** the VFS is initialized
- **THEN** a LocalBlobStore is instantiated with the specified base path

#### Scenario: S3Adapter

- **GIVEN** blob_store_uri="s3://my-bucket/aifs"
- **WHEN** the VFS is initialized
- **THEN** an S3BlobStore is instantiated

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
Cache SHALL be enabled by default for remote stores (S3, Azure) and disabled for local FS.

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

### Requirement: MetadataCASSemantics

The MetadataStore SHALL implement compare-and-swap semantics for version mutations.
SQL adapters SHALL use `WHERE version_number = ?` returning zero rows on mismatch.
NoSQL adapters SHALL use atomic find-and-update with version matching.

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

- **GIVEN** AIFS_METADATA_STORE_URI is set to "postgresql://..."
- **WHEN** VFSConfig is constructed without arguments
- **THEN** the Postgres URI is used

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

## Technical Notes

- **Implementation**: src/aifs/protocols/ (protocol definitions), src/aifs/stores/ (adapters), src/aifs/config.py
- **Dependencies**: none (storage is the foundational layer)
- **Initial adapters**: SQLite (aiosqlite), local FS (aiofiles), diskcache.
  Postgres (asyncpg), Mongo (motor), S3 (aiobotocore) are optional dependencies.
