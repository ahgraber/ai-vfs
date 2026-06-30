# Session — Spec

## Requirements

### Requirement: CWDState

A Session SHALL maintain a current working directory (`cwd`) scoped within a single namespace.
The `cwd` SHALL default to `"/"` on construction and SHALL always be an absolute path.

#### Scenario: DefaultCWD

- **GIVEN** a new Session is constructed with a namespace and principal
- **WHEN** `pwd()` is called immediately
- **THEN** `"/"` is returned

#### Scenario: CWDIsAbsolute

- **GIVEN** any Session state
- **WHEN** `cwd` is inspected
- **THEN** it begins with `"/"`

### Requirement: RelativePathResolution

The Session SHALL resolve all path arguments through the current `cwd` before passing them to the VFS.
Absolute paths SHALL pass through unchanged (after normalization).
Relative paths SHALL be joined with `cwd` and normalized using POSIX path semantics.

#### Scenario: RelativePathResolved

- **GIVEN** `cwd` is `"/src/"`
- **WHEN** a principal reads `"./utils.py"`
- **THEN** the VFS receives `"/src/utils.py"`

#### Scenario: DotDotResolved

- **GIVEN** `cwd` is `"/src/app/"`
- **WHEN** a principal writes to `"../config.py"`
- **THEN** the VFS receives `"/src/config.py"`

#### Scenario: AbsolutePathPassthrough

- **GIVEN** `cwd` is `"/src/"`
- **WHEN** a principal stats `"/data/file.txt"`
- **THEN** the VFS receives `"/data/file.txt"` (unchanged)

### Requirement: PathTraversalPrevention

The path resolution algorithm SHALL guarantee resolved paths cannot escape the namespace root `"/"`.
Traversal above `"/"` SHALL be silently clamped (POSIX normpath behavior: `normpath("/../x") == "/x"`).

#### Scenario: TraversalClampedAtRoot

- **GIVEN** `cwd` is `"/"`
- **WHEN** a principal accesses `"../../../../etc/passwd"`
- **THEN** the resolved path is `"/etc/passwd"` (not above `"/"`)
- **AND** the VFS permission check determines whether access is allowed

### Requirement: CdOperation

The Session SHALL provide a `cd(path)` operation that updates `cwd`.
The path (absolute or relative) SHALL be resolved, then validated for read permission.
If the principal lacks read permission on the target, `PermissionDeniedError` SHALL be raised and `cwd` SHALL remain unchanged.

#### Scenario: CdAbsolute

- **GIVEN** the principal has read permission on `"/workspace/"`
- **WHEN** `cd("/workspace/")` is called
- **THEN** `cwd` is updated to `"/workspace/"`

#### Scenario: CdRelative

- **GIVEN** `cwd` is `"/workspace/"` and the principal has read permission on `"/workspace/src/"`
- **WHEN** `cd("src/")` is called
- **THEN** `cwd` is updated to `"/workspace/src/"`

#### Scenario: CdPermissionDenied

- **GIVEN** the principal has no read permission on `"/secret/"`
- **WHEN** `cd("/secret/")` is called
- **THEN** `PermissionDeniedError` is raised and `cwd` is unchanged

#### Scenario: CdDotDot

- **GIVEN** `cwd` is `"/workspace/src/"`
- **WHEN** `cd("..")` is called
- **THEN** `cwd` is updated to `"/workspace/"`

#### Scenario: CdAtRoot

- **GIVEN** `cwd` is `"/"`
- **WHEN** `cd("..")` is called
- **THEN** `cwd` remains `"/"`

### Requirement: PwdOperation

The Session SHALL provide a `pwd()` operation that returns the current `cwd` string.

#### Scenario: PwdReflectsCd

- **GIVEN** `cd("/workspace/")` was called successfully
- **WHEN** `pwd()` is called
- **THEN** `"/workspace/"` is returned

### Requirement: SessionProxiesVFS

The Session SHALL expose the same file operations as VFS (`read`, `write`, `delete`, `stat`, `list`, `search`, `versions`, `rollback`, `copy`, `move`, `execute`), each resolving path arguments through `cwd` before delegating to the underlying VFS instance.
`session.execute(code, ...)` SHALL resolve the caller's namespace and principal from the session context and delegate to `vfs.execute` with the session's current `cwd` as the `cwd` argument.
The `execute` permission check is performed inside `vfs.execute`, not on the `Session`; the `FsOperations` instance is constructed inside `vfs.execute`, not on the `Session`.

#### Scenario: AllPathArgsResolved

- **GIVEN** `cwd` is `"/workspace/"`
- **WHEN** a principal calls `session.list("src/")` (relative)
- **THEN** the VFS receives `list("/workspace/src/")`

#### Scenario: CopyBothPathsResolved

- **GIVEN** `cwd` is `"/workspace/"`
- **WHEN** a principal calls `session.copy("a.py", "../archive/a.py")`
- **THEN** the VFS receives `copy("/workspace/a.py", "/archive/a.py")`

#### Scenario: SessionExecuteProxiesToVfs

- **GIVEN** a `Session` constructed with `namespace_id` and `principal_id`
- **WHEN** `session.execute(code, provider_name="monty", ...)` is called
- **THEN** `vfs.execute(code, namespace_id=session.namespace_id, principal_id=session.principal_id, cwd=session.cwd, ...)` is invoked

### Requirement: SessionSearch

The `Session.search` method SHALL accept a `find_predicates` passthrough parameter: `session.search(query, scope, search_type, find_predicates=None)`.
The `find_predicates` value SHALL be forwarded to the underlying `vfs.search` call unchanged; all other parameters and behavior are unchanged.

#### Scenario: FindPredicatesPassthrough

- **GIVEN** a `Session` and a `FindPredicates` value
- **WHEN** `session.search(query, scope, search_type, find_predicates=pred)` is called
- **THEN** `vfs.search` is invoked with the same `find_predicates` value forwarded unchanged

### Requirement: PublicApiSurface

The package SHALL expose `Session` and `resolve_path` as top-level imports from `vfs` so callers can construct a session without reaching into submodules.

#### Scenario: TopLevelImport

- **GIVEN** the `vfs` package is installed
- **WHEN** a caller executes `from vfs import Session, resolve_path`
- **THEN** the import succeeds and both names are present in `vfs.__all__`

## Technical Notes

- **Resolution algorithm**: `posixpath.join(cwd, input_path)` → `posixpath.normpath(result)`, with the input's trailing `/` preserved when present (so directory-style arguments reach the VFS as directory prefixes)
- **Ephemeral state**: `cwd` is in-memory only; resets to `"/"` on Session construction
- **No directory existence check on `cd`**: VFS directories are implicit (path prefixes);
  permission check alone gates `cd`
- **`cd` directory-prefix normalization**: `cd` appends a trailing `/` to the resolved target (except for root `/`) so the stored `cwd` matches the conventional shape of permission `path_prefix` values
- **Dependencies**: file-operations (VFS operations), access-control (permission check in `cd`)
