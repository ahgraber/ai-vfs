# Access Control Specification

> Generated from design document analysis on 2026-04-04
> Source files: docs/specs/2026-04-04-ai-vfs-design.md (Sections 5, 2.1)

## Purpose

Path-based access control with default-deny semantics and invisible pruning.
Principals are granted operations on namespace path prefixes.
Unauthorized paths are invisible — they do not appear in listings or search results.

## Requirements

### Requirement: DefaultDeny

The system SHALL deny all operations for a principal that has no matching permission entry.

#### Scenario: NoPrincipalPermission

- **GIVEN** a principal with no permissions on a namespace
- **WHEN** the principal attempts to read any path
- **THEN** a PermissionDeniedError is raised

### Requirement: PathPrefixPermissions

The system SHALL evaluate permissions by matching the requested path against
permission entries' path_prefix fields, using most-specific-prefix-first ordering.

#### Scenario: MostSpecificPrefixWins

- **GIVEN** principal has read-only on "/" and read-write on "/workspace/"
- **WHEN** the principal writes to "/workspace/file.txt"
- **THEN** the write is allowed (the /workspace/ rule is more specific)

#### Scenario: BroadRuleApplies

- **GIVEN** principal has read-only on "/" and read-write on "/workspace/"
- **WHEN** the principal writes to "/config.yaml"
- **THEN** the write is denied (only the broad "/" rule matches, which is read-only)

### Requirement: InvisiblePruning

The system SHALL exclude unauthorized paths from list and stat results.
An agent SHALL NOT be able to discover or reference paths it cannot read.

#### Scenario: ListExcludesUnauthorized

- **GIVEN** files /public/a.txt and /secret/b.txt exist
- **WHEN** a principal with read permission only on /public/ lists /
- **THEN** only /public/a.txt appears; /secret/b.txt is invisible

#### Scenario: SearchScopedToPermissions

- **GIVEN** files matching a search query exist in both /public/ and /secret/
- **WHEN** a principal with read permission only on /public/ searches
- **THEN** only matches in /public/ are returned

### Requirement: NamespaceBoundary

The system SHALL enforce complete isolation between namespaces.
Cross-namespace access requires an explicit permission entry for the foreign namespace.

#### Scenario: CrossNamespaceDenied

- **GIVEN** principal has permissions only in namespace A
- **WHEN** the principal attempts any operation in namespace B
- **THEN** a PermissionDeniedError is raised

### Requirement: OperationGranularity

The system SHALL support the following operation types: read, write, delete, execute, and admin.
The admin operation SHALL grant permission management on the associated subtree.

#### Scenario: ReadOnlyPrincipal

- **GIVEN** a principal with only {read} operations
- **WHEN** the principal attempts a write
- **THEN** a PermissionDeniedError is raised

### Requirement: PermissionGranting

The system SHALL allow principals with admin permission to grant or modify permissions on their subtree.

#### Scenario: GrantPermission

- **GIVEN** a principal with admin on /
- **WHEN** that principal grants read+write on /workspace/ to another principal
- **THEN** the other principal can read and write under /workspace/

### Requirement: HumanFriendlyNames

The system SHALL maintain a names table mapping ULIDs to human-friendly display names for namespaces, principals, and other entities.
The VFS API SHALL accept either ULID or display name, resolving names to ULIDs at the boundary.

#### Scenario: ResolveNameToULID

- **GIVEN** a namespace with ULID "01JQX..." and display_name "my-workspace"
- **WHEN** a name lookup for "my-workspace" is performed
- **THEN** the ULID "01JQX..." is returned

## Technical Notes

- **Implementation**: src/aifs/vfs.py (\_check_perm), src/aifs/stores/sqlite_metadata.py (check_permission, set_permission)
- **Dependencies**: storage (MetadataStore for permission queries)
- **Future expansion**: RBAC with roles mapping to operation sets; group principals.
  No schema change needed — operations set already encodes what roles would expand to.
