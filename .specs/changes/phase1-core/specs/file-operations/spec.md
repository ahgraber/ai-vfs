# File Operations — Delta Spec

> Change: `phase1-core`
> Date: 2026-04-04

## ADDED Requirements

### Requirement: ContentAddressedStorage

The system SHALL store file content in the blob store keyed by BLAKE3 hash
of the content bytes, so that identical content is stored only once.

#### Scenario: DeduplicatedWrite

- **GIVEN** two files with identical content exist in different paths
- **WHEN** both files are written to the VFS
- **THEN** only one blob object exists in the blob store

#### Scenario: DeterministicHash

- **GIVEN** the same byte sequence is hashed twice
- **WHEN** BLAKE3 content_hash is computed
- **THEN** both invocations return the same 64-character lowercase hex string

### Requirement: WriteCreatesVersion

The system SHALL create a new immutable version record for every write operation,
incrementing the per-file monotonic version number.

#### Scenario: FirstWrite

- **GIVEN** a path that does not exist in the namespace
- **WHEN** a principal writes content to that path
- **THEN** a file record is created with version_number 1, and a version record is persisted with the content hash and size

#### Scenario: SubsequentWrite

- **GIVEN** a file at version N
- **WHEN** a principal writes new content
- **THEN** a new version N+1 is created; the file's current_version_id and current_version_number are updated

### Requirement: ReadReturnsContent

The system SHALL return the blob content for a file's current version by default,
or for a specific version when version_number is provided.

#### Scenario: ReadLatest

- **GIVEN** a file with versions 1, 2, and 3
- **WHEN** a principal reads the file without specifying a version
- **THEN** the content of version 3 is returned

#### Scenario: ReadSpecificVersion

- **GIVEN** a file with versions 1, 2, and 3
- **WHEN** a principal reads the file with version_number=1
- **THEN** the content of version 1 is returned

#### Scenario: ReadNonexistent

- **GIVEN** a path that does not exist
- **WHEN** a principal reads that path
- **THEN** a NotFoundError is raised

### Requirement: LazyContentResolution

The system SHALL NOT fetch blob content for list, stat, or versions operations; only read SHALL access the blob store.

#### Scenario: StatMetadataOnly

- **GIVEN** a file exists
- **WHEN** a principal calls stat
- **THEN** file metadata (path, version number, size, timestamps) is returned without any blob store access

#### Scenario: ListMetadataOnly

- **GIVEN** multiple files exist under a path prefix
- **WHEN** a principal calls list
- **THEN** file metadata entries are returned without any blob store access

### Requirement: DeleteCreatesTombstone

The system SHALL mark a file as deleted by creating a tombstone version,
preserving all prior versions for potential rollback.

#### Scenario: DeleteFile

- **GIVEN** a file at version N
- **WHEN** a principal deletes the file
- **THEN** a tombstone version N+1 is created, the file is marked is_deleted=True, and subsequent reads raise NotFoundError

#### Scenario: DeletedFileVersionsAccessible

- **GIVEN** a deleted file
- **WHEN** a principal lists versions of that file
- **THEN** all versions including the tombstone are returned

### Requirement: ListDirectoryContents

The system SHALL list files under a path prefix, with optional recursion.

> **Note:** Glob/pattern-based listing (e.g., `*.py` under `/src/`) is intentionally excluded here; it is expected to be handled by the search layer (`GlobSearch` in the search spec).

#### Scenario: NonRecursiveList

- **GIVEN** files at /src/a.py, /src/b.py, and /src/sub/c.py
- **WHEN** a principal lists /src/ non-recursively
- **THEN** only /src/a.py and /src/b.py are returned

#### Scenario: RecursiveList

- **GIVEN** files at /src/a.py, /src/b.py, and /src/sub/c.py
- **WHEN** a principal lists /src/ recursively
- **THEN** all three files are returned

#### Scenario: ListExcludesDeleted

- **GIVEN** a deleted file and a live file under the same prefix
- **WHEN** a principal lists that prefix
- **THEN** only the live file appears

### Requirement: CopyFile

The system SHALL copy a file to a new path within the same namespace, creating a new file record at version 1 pointing at the same blob.
The source file is unchanged.
Because blobs are content-addressed, no additional blob storage is consumed when the content is identical.

#### Scenario: CopyToNewPath

- **GIVEN** a file at /src/a.py with content hash H at version 3
- **WHEN** a principal copies /src/a.py to /dst/a.py
- **THEN** a new file record exists at /dst/a.py with version 1 and content hash H, and /src/a.py remains at version 3

#### Scenario: CopyToExistingPath

- **GIVEN** a file already exists at the destination path
- **WHEN** a principal copies to that path
- **THEN** the destination is overwritten (a new version is written), consistent with write semantics

#### Scenario: CopyNonexistentSource

- **GIVEN** the source path does not exist
- **WHEN** a principal issues a copy
- **THEN** a NotFoundError is raised and no destination record is created

### Requirement: MoveFile

The system SHALL move (rename) a file to a new path within the same namespace as an atomic operation: the source receives a tombstone and the destination is created at version 1 with the same content hash.
Version history is not transferred; the destination begins a new version chain.

#### Scenario: MoveToNewPath

- **GIVEN** a file at /src/a.py at version 5
- **WHEN** a principal moves /src/a.py to /dst/a.py
- **THEN** /dst/a.py exists at version 1 with the same content hash, and /src/a.py has a tombstone (is_deleted=True)

#### Scenario: MoveTombstoneAndCreateAreAtomic

- **GIVEN** a move operation is in progress
- **WHEN** any failure occurs mid-operation
- **THEN** the system leaves neither a partial destination nor an unintended tombstone on the source

#### Scenario: MoveToExistingPath

- **GIVEN** a file already exists at the destination path
- **WHEN** a principal moves to that path
- **THEN** the destination is overwritten and the source receives a tombstone

#### Scenario: MoveNonexistentSource

- **GIVEN** the source path does not exist
- **WHEN** a principal issues a move
- **THEN** a NotFoundError is raised and no destination record is created

### Requirement: OptimisticConcurrency

The system SHALL support optimistic concurrency via an optional expected_version parameter on write.
When provided, the write SHALL fail with ConflictError if the file's current version does not match.

#### Scenario: ConcurrentWriteConflict

- **GIVEN** a file at version 2
- **WHEN** a principal writes with expected_version=1
- **THEN** a ConflictError is raised with expected=1, actual=2

#### Scenario: ConcurrentWriteSuccess

- **GIVEN** a file at version 2
- **WHEN** a principal writes with expected_version=2
- **THEN** version 3 is created successfully

#### Scenario: WriteWithoutExpectedVersion

- **GIVEN** a file at any version
- **WHEN** a principal writes without expected_version
- **THEN** the write succeeds (last-writer-wins)

### Requirement: NamespaceIsolation

The system SHALL scope all file operations to a namespace.
Files in one namespace SHALL NOT be visible or accessible from another namespace.

#### Scenario: CrossNamespaceInvisible

- **GIVEN** a file /data.txt in namespace A
- **WHEN** a principal lists / in namespace B
- **THEN** /data.txt does not appear

### Requirement: ULIDIdentifiers

The system SHALL choose identifier type based on temporal-information exposure risk:

- **UUID4** (fully random, no embedded timestamp): any person-related entity, or any entity whose ID may be exposed externally and where leaking temporal information — concrete (exact creation time) or relative (creation ordering between two entities) — could give an attacker exploitable information.
  Current example: `Principal.id`.

- **ULID** (time-sortable, timestamp in high bits): file-system entities and internal metadata where temporal sortability aids debugging and log correlation, and the IDs are not expected to leak person-related timing signals.
  Current examples: `Namespace.id`, `VersionMeta.id`, `Permission.id`, `AuditEvent.event_id`.

The per-file monotonic `version_number` integer continues to serve as the human-facing version identifier.

When a new entity type is introduced, the implementer SHALL evaluate whether a
time-sortable ID would expose concrete or relative timing information to an external
caller, and use UUID4 if so.

#### Scenario: PrincipalIdIsFullyRandom

- **GIVEN** a new principal is created
- **WHEN** the principal record is inspected
- **THEN** `principal.id` is a UUID4 string with no embedded timestamp, so that neither
  the creation time nor the relative creation order of two principals can be inferred
  from their IDs

#### Scenario: FileEntityIdIsULID

- **GIVEN** a new namespace or version is created
- **WHEN** the entity record is inspected
- **THEN** the entity `id` is a 26-character ULID string encoding the creation timestamp

#### Scenario: VersionDualIdentifier

- **GIVEN** a new version is created
- **WHEN** the version record is inspected
- **THEN** it has both a globally unique ULID `id` and a per-file `version_number` integer
