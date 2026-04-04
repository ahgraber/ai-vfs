# File Operations Specification (Delta)

**Change:** `shell-context` **Date:** 2026-04-04

## ADDED

### Requirement: AbsolutePathsOnly

The VFS SHALL reject any path argument that is not absolute (does not begin with `"/"`), raising `ValueError` immediately, before any permission check or storage access.
Relative path resolution is the responsibility of the caller (e.g., `Session`).

#### Scenario: RelativePathRejected

- **GIVEN** a VFS instance
- **WHEN** any operation is called with a path that does not begin with `"/"`
- **THEN** a `ValueError` is raised with a message indicating the path must be absolute

#### Scenario: AbsolutePathAccepted

- **GIVEN** a VFS instance
- **WHEN** any operation is called with a path beginning with `"/"`
- **THEN** the operation proceeds normally (path is not rejected at the boundary)
