# Access Control — Spec

## Requirements

### Requirement: DefaultDeny

The system SHALL deny all operations for a principal that has no matching permission entry.

#### Scenario: NoPrincipalPermission

- **GIVEN** a principal with no permissions on a namespace
- **WHEN** the principal attempts to read any path
- **THEN** a PermissionDeniedError is raised

### Requirement: PathPrefixPermissions

The system SHALL evaluate permissions by matching the requested path against permission
entries' `path_prefix` fields using **segment-boundary precision**, with
most-specific-prefix-first (longest prefix) ordering.

A prefix P matches path X if and only if:

- `X == P` (exact match — covers single-file grants), or
- `X` starts with `P + "/"` when P does not end with `"/"`, or
- `X` starts with `P` when P already ends with `"/"`.

This ensures that a grant on `/work` covers `/work` and `/work/file.txt` but does **not** cover `/workspace/file.txt`.

The system SHALL validate that any `path_prefix` stored via `set_permission` or
`grant` is canonical (absolute and normpath-equal after stripping at most one trailing
`"/"`), raising `ValueError` otherwise.

#### Scenario: MostSpecificPrefixWins

- **GIVEN** principal has read-only on "/" and read-write on "/workspace/"
- **WHEN** the principal writes to "/workspace/file.txt"
- **THEN** the write is allowed (the /workspace/ rule is more specific)

#### Scenario: BroadRuleApplies

- **GIVEN** principal has read-only on "/" and read-write on "/workspace/"
- **WHEN** the principal writes to "/config.yaml"
- **THEN** the write is denied (only the broad "/" rule matches, which is read-only)

#### Scenario: SegmentBoundaryNotBypassed

- **GIVEN** principal has read-write on "/work"
- **WHEN** the principal attempts to read "/workspace/file.txt"
- **THEN** the read is denied (the "/work" grant does not cover "/workspace/")

#### Scenario: ExactGrantCoversFile

- **GIVEN** principal has read-write on "/work"
- **WHEN** the principal reads "/work"
- **THEN** the read is allowed (exact match on the grant path)

#### Scenario: DirectoryGrantCoversChildren

- **GIVEN** principal has read-write on "/work/"
- **WHEN** the principal reads "/work/file.txt"
- **THEN** the read is allowed (path falls under the /work/ subtree)

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
The system SHALL enforce the `execute` permission at the `vfs.execute` entry point.
A principal that does not have `execute` permission on the provided `cwd` SHALL cause `vfs.execute` to raise `PermissionDeniedError` before any session, `FsOperations`, or provider is constructed — consistent with every other VFS operation.
All other operation types (`read`, `write`, `delete`, `admin`) are unchanged.

#### Scenario: ReadOnlyPrincipal

- **GIVEN** a principal with only {read} operations
- **WHEN** the principal attempts a write
- **THEN** a PermissionDeniedError is raised

#### Scenario: ExecutePermissionEnforced

- **GIVEN** a principal with `{read, write}` but not `{execute}` on `/workspace/`
- **WHEN** `vfs.execute(code, namespace_id, principal_id, ..., cwd="/workspace/")` is called
- **THEN** `PermissionDeniedError` is raised immediately — no session is created, no FsOperations
  is constructed, and no provider dispatch occurs

#### Scenario: ExecutePermissionStorable

- **GIVEN** an admin grants {execute} on /workspace/ to a principal
- **WHEN** the permission is persisted and queried
- **THEN** the {execute} operation is present in the stored operations set

### Requirement: PermissionGranting

The system SHALL allow principals with admin permission on a path prefix to grant or modify permissions on that subtree.
The system SHALL deny permission-granting attempts by principals that lack admin on the target subtree.
The system SHALL provide a one-time bootstrap mechanism to create the initial admin in an empty namespace, since admin-gated granting is otherwise unreachable from a permissionless starting state.

#### Scenario: GrantPermission

- **GIVEN** a principal with admin on /
- **WHEN** that principal grants read+write on /workspace/ to another principal
- **THEN** the other principal can read and write under /workspace/

#### Scenario: NonAdminCannotGrant

- **GIVEN** a principal with read+write but no admin on /workspace/
- **WHEN** that principal attempts to grant any operation on /workspace/ to another principal
- **THEN** a PermissionDeniedError is raised and the permissions table is unchanged

#### Scenario: BootstrapInitialAdmin

- **GIVEN** a namespace with no admin principals
- **WHEN** a caller invokes the bootstrap mechanism to grant admin on / to a principal
- **THEN** that principal holds admin on / and can subsequently grant further permissions via the normal admin-gated path
- **AND** subsequent bootstrap invocations on the same namespace are rejected (single-use guard)

### Requirement: HumanFriendlyNames

The system SHALL maintain a names table mapping entity identifiers (UUID4 or ULID, depending on the entity type's privacy classification) to human-friendly display names for namespaces, principals, and other entities.
The VFS API SHALL expose a `resolve_name(entity_type, display_name)` helper that returns the underlying identifier (or `None`) for a given display name.
Callers are responsible for translating display names to identifiers at their own boundary before invoking other VFS operations.
The names table stores identifiers as opaque text regardless of format.

> **Note:** VFS operations (`stat`, `read`, `write`, etc.) accept raw identifiers only.
> Auto-resolution of display names at the operation boundary is not implemented; the lookup-helper pattern is the explicit, predictable primitive.
> A higher-level service surface (Phase 3 RPC/MCP layer) may add request-time name resolution above this API.

#### Scenario: ResolveNameToULID

- **GIVEN** a namespace with ULID `"01JQX..."` and `display_name="my-workspace"`
- **WHEN** a name lookup for `"my-workspace"` is performed
- **THEN** the ULID `"01JQX..."` is returned

#### Scenario: ResolveNameToUUID4

- **GIVEN** a principal with UUID4 `"550e8400-..."` and `display_name="agent-bob"`
- **WHEN** a name lookup for `"agent-bob"` is performed
- **THEN** the UUID4 `"550e8400-..."` is returned
