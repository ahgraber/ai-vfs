# Storage — Delta Spec

> Change: `phase2-adapters`
> Date: 2026-04-04

## ADDED Requirements

### Requirement: PostgresMetadataAdapter

The system SHALL provide a PostgresMetadataStore implementing the MetadataStore
protocol, using `asyncpg` for async Postgres access with JSONB columns for
extensible fields (`search_meta`, `detail`).

#### Scenario: PostgresAdapter

- **GIVEN** metadata_store_uri="postgresql://localhost/aifs"
- **WHEN** the VFS is initialized
- **THEN** a PostgresMetadataStore is instantiated and tables are created

### Requirement: MongoMetadataAdapter

The system SHALL provide a MongoMetadataStore implementing the MetadataStore
protocol, using `motor` for async MongoDB access with native document storage
for extensible fields.

#### Scenario: MongoAdapter

- **GIVEN** metadata_store_uri="mongodb://localhost/aifs"
- **WHEN** the VFS is initialized
- **THEN** a MongoMetadataStore is instantiated

### Requirement: S3BlobAdapter

The system SHALL provide an S3BlobStore implementing the BlobStore protocol,
using `aiobotocore` for async S3-compatible object storage with content-hash keying.

#### Scenario: S3Adapter

- **GIVEN** blob_store_uri="s3://my-bucket/aifs"
- **WHEN** the VFS is initialized
- **THEN** an S3BlobStore is instantiated

## MODIFIED Requirements

### Requirement: MetadataCASSemantics

The MetadataStore SHALL implement compare-and-swap semantics for version mutations.
SQL adapters SHALL use `WHERE version_number = ?` returning zero rows on mismatch.
NoSQL adapters SHALL use atomic find-and-update with version matching. (Previously: only SQL adapter CAS behavior was specified.)

#### Scenario: CASConflictDetected

- **GIVEN** a file at version 5
- **WHEN** put_version is called with expected_version=3
- **THEN** a ConflictError is raised

#### Scenario: MongoCASConflict

- **GIVEN** a file at version 5 in MongoMetadataStore
- **WHEN** put_version is called with expected_version=3
- **THEN** a ConflictError is raised via `find_one_and_update` with version match
