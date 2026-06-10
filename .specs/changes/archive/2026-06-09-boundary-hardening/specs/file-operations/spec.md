# File Operations — Delta Spec

> Change: `boundary-hardening`
> Date: 2026-06-09

## MODIFIED Requirements

### Requirement: AbsolutePathsOnly

> Previously: only checked `path.startswith("/")`.

The VFS SHALL reject any path argument that is not **canonical**: it must begin with `"/"` and, after stripping at most one trailing `"/"` (the root `"/"` is exempt from stripping), it must equal `posixpath.normpath(path)`.
Any path containing `..`, `.` segments, or repeated slashes SHALL be rejected with `ValueError` immediately, before any permission check or storage access.

A single trailing `"/"` is permitted so that directory-style arguments forwarded by
`Session` (e.g. `cd` always appends `"/"`) are accepted.

Relative path resolution remains the responsibility of the caller (e.g. `Session`).

#### Scenario: RelativePathRejected

- **GIVEN** a VFS instance
- **WHEN** any operation is called with a path that does not begin with `"/"`
- **THEN** a `ValueError` is raised with a message indicating the path must be absolute

#### Scenario: DotDotPathRejected

- **GIVEN** a VFS instance
- **WHEN** any operation is called with a path containing `..` (e.g. `/public/../secret/x`)
- **THEN** a `ValueError` is raised before any permission check or storage access

#### Scenario: DoubleSlashRejected

- **GIVEN** a VFS instance
- **WHEN** any operation is called with a path containing repeated slashes (e.g. `/foo//bar`)
- **THEN** a `ValueError` is raised before any permission check or storage access

#### Scenario: TrailingSlashDirectoryArgAccepted

- **GIVEN** a VFS instance
- **WHEN** `list` or `search` is called with a directory-style path like `/src/`
- **THEN** the operation proceeds normally (a single trailing slash is not rejected)

#### Scenario: AbsolutePathAccepted

- **GIVEN** a VFS instance
- **WHEN** any operation is called with a clean absolute path beginning with `"/"`
- **THEN** the operation proceeds normally (path is not rejected at the boundary)

### Requirement: OptimisticConcurrency

> Previously: did not specify behaviour for concurrent no-CAS writes that collide on
> version number.

The system SHALL support optimistic concurrency via an optional `expected_version` parameter on write.
When provided, the write SHALL fail with `ConflictError` if the file's current version does not match.

When `expected_version` is **not** provided (last-writer-wins), two concurrent writers reading the same current version N and both attempting to write version N+1 SHALL both ultimately succeed, producing versions N+1 and N+2 respectively, because the VFS retries the read-compute-put loop on a version-number collision (`VersionCollisionError`), bounded at 5 attempts.
If the retry budget is exhausted, `VersionCollisionError` is raised.

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

#### Scenario: NoCASWriteCollisionBothSucceed

- **GIVEN** two concurrent writers, both reading the file at version N before either writes
- **WHEN** both write without expected_version
- **THEN** both writes succeed: one lands as version N+1, the other retries and lands as N+2
