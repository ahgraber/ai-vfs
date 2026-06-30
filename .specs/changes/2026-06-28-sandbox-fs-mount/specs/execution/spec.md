# Delta for execution

> This change adds a native filesystem mount and a second sandbox provider so sandboxed code
> reads and writes the governed VFS through native file I/O.
> Mechanism (the `AbstractOS` sync→async bridge, `commands=` overrides) lives in `design.md`.
> Hash-anchored editing is removed here and deferred to a future change at
> `.specs/changes/2026-06-30-anchored-editing/`.

## ADDED Requirements

### Requirement: FsPortContract

The system SHALL define an **FS-port**: an async, path-based filesystem interface, backed by a `Session`, exposing whole-file `read`, `write`, `list`, `stat`, `exists`, and `delete`, plus `mkdir` as a no-op over implicit directories.
Every FS-port operation SHALL route through the bound `Session`, so the principal's permissions are enforced and state-changing operations are audited exactly as for direct VFS calls.
The FS-port SHALL NOT expose the host operating system's filesystem.
A filesystem operation that has no VFS equivalent — symbolic links, permission-mode changes, modification-time changes — SHALL raise an unsupported-operation error rather than silently succeed.

Serves: governed-mount, portable-sandboxes

#### Scenario: FsPortReadWriteRouteThroughSession

- **GIVEN** an FS-port bound to a `(namespace, principal)` with read+write permission
- **WHEN** `write` then `read` are called for a path
- **THEN** the write creates a new VFS version and the read returns its content — both subject
  to the principal's permission checks

#### Scenario: FsPortRejectsUnauthorizedPath

- **GIVEN** an FS-port whose principal lacks read permission on a path
- **WHEN** `read` is called for that path
- **THEN** `PermissionDeniedError` is raised

#### Scenario: FsPortMkdirIsNoOp

- **GIVEN** an FS-port over the VFS (directories are implicit prefixes)
- **WHEN** `mkdir` is called for a path
- **THEN** the call succeeds without creating a directory entity, and writing a file under that
  prefix later still works

#### Scenario: FsPortUnsupportedOperationRaises

- **GIVEN** an FS-port
- **WHEN** a symlink, mode-change, or mtime-change operation is requested
- **THEN** an unsupported-operation error is raised (the operation is not silently accepted)

### Requirement: MontyNativeFilesystemMount

The system SHALL mount the FS-port into the Monty sandbox so that the sandboxed code's native filesystem operations (`open`, `pathlib.Path`, `os` path calls) resolve to FS-port operations on the governed VFS.
The mount SHALL be an interpreter-level virtual filesystem — a proxy into the VFS — and SHALL NOT attach or expose any host operating-system filesystem to the sandbox.
Because Monty dispatches these filesystem callbacks off the host event-loop thread, the mount SHALL bridge each synchronous callback to the asynchronous FS-port without blocking the host event loop and without losing the VFS's permission and audit enforcement.
A VFS error raised inside a bridged callback (e.g. `PermissionDeniedError`, `NotFoundError`) SHALL retain its identity through Monty's exception downcast, so `vfs.execute`'s error-translation table maps it to its structured `error_type` rather than a generic provider/internal error.

Serves: monty-code-mode, governed-mount

#### Scenario: NativeOpenReadsVfsFile

- **GIVEN** a file exists in the VFS and the principal has read permission
- **WHEN** sandboxed Monty code calls `open("/path").read()` (or `Path("/path").read_text()`)
- **THEN** the VFS file's current content is returned to the sandbox

#### Scenario: NativeWritePersistsVersion

- **GIVEN** the principal has write permission on a path
- **WHEN** sandboxed Monty code writes to that path via native filesystem calls — both
  `open(path, "w").write(...)` (the append-callback path) and `pathlib.Path.write_text`
- **THEN** a new VFS version is created with the written content

#### Scenario: MountEnforcesPermissions

- **GIVEN** the principal lacks read permission on a path
- **WHEN** sandboxed code attempts a native read of that path
- **THEN** the read fails (permission denied); the mount does not bypass access control

#### Scenario: NativeMountDenialTranslatesToPermissionDenied

- **GIVEN** a principal that lacks read permission on a path
- **WHEN** sandbox code performs a native read of it through the mount and the resulting failure
  reaches `vfs.execute`
- **THEN** the structured result has `error_type="permission_denied"` (the VFS error survived
  Monty's downcast), not `provider_error`/`internal_error`, and `error_message` carries no host path

#### Scenario: MountDoesNotExposeHostFilesystem

- **GIVEN** a mounted sandbox
- **WHEN** sandboxed code attempts to read a host path that is not a VFS path
- **THEN** the host filesystem is not reachable through the mount

#### Scenario: HostEventLoopNotBlockedDuringNativeFsCalls

- **GIVEN** a concurrent `asyncio.Task` recording heartbeat ticks
- **WHEN** sandboxed code performs native filesystem operations through the mount
- **THEN** the heartbeat task keeps ticking (the host event loop is not blocked by the bridge)

### Requirement: JustBashProvider

The system SHALL provide a just-bash execution provider that runs bash over the governed VFS by injecting an FS-port-backed filesystem, so that bash builtins (`cat`, `ls`, pipes, redirection) operate on VFS files with the principal's permissions enforced.
The provider SHALL replace the `grep`, `find`, and `glob` builtins so they resolve to the VFS search index — parity with the Monty search verbs — rather than brute-force file enumeration.
Filesystem operations with no VFS equivalent SHALL raise unsupported, consistent with the FS-port.

Serves: just-bash-shell-tool, portable-sandboxes

#### Scenario: BashCatReadsVfsFile

- **GIVEN** a file exists in the VFS and the principal has read permission
- **WHEN** sandboxed bash runs `cat /path`
- **THEN** the VFS file's current content is produced on stdout

#### Scenario: BashWritePersistsVersion

- **GIVEN** the principal has write permission
- **WHEN** sandboxed bash writes a file (e.g. redirection `> /path`)
- **THEN** a new VFS version is created with the written content

#### Scenario: GrepRoutesToSearchIndex

- **GIVEN** a metadata store exposing the native search index and matching indexed files
- **WHEN** sandboxed bash runs `grep PATTERN /scope`
- **THEN** results come from the VFS search index (the overridden builtin), not brute-force
  enumeration of every file

#### Scenario: BashRespectsPermissions

- **GIVEN** the principal lacks read permission on a path
- **WHEN** sandboxed bash runs `cat` on that path
- **THEN** the read is denied; the bash provider does not bypass access control

## MODIFIED Requirements

### Requirement: ShellOperationsLayer

The `FsOperations` shell wrappers SHALL implement the following dispatch:

- `grep(pattern, path, recursive=True)` → `session.search(query=pattern, scope=resolved_path, search_type=SearchType.REGEX)`, returning structured match dicts; re-raises `ReadBudgetExceededError`, `ReindexRequiredError`, and `IndexUnavailableError` unchanged.
  When `recursive=False`, results are post-filtered to depth-1 files only (files whose path has no additional `/` segment after the resolved scope); the underlying `session.search` always scans recursively.
- `find(path, **predicates)` → `session.search(scope=path, search_type=FIND, find_predicates=FindPredicates(**predicates))`.
- `glob(pattern)` → `session.search` with `search_type=GLOB`.
- `cat(path)` → `session.read(path)` decoded as strict UTF-8; undecodable content yields a structured error.
  Returns `{"lines": [...], "error": None}`; content split on `\n` only (`\r` kept in line content); trailing-newline presence preserved.
  A path beginning with `//` is accepted as a canonical absolute path by the POSIX path resolver and is permission-checked like any other absolute path.
  Raises a structured error (not host OOM) if content exceeds `resource_limits.max_read_bytes`; the size check is performed via `stat` before the blob is fetched when `max_read_bytes` is set.
- `head(path, n)` / `tail(path, n)` → same UTF-8 decode and line model as `cat`, returning the sliced lines as `{"lines": [...], "error": None}`.
- `ls(path)` → `session.list(path)` mapped to a list of dicts with fields `name`, `path`, `is_dir` (synthesized for implicit directory prefixes via an internal recursive scan), `version_number`, and `updated_at`.
  `size` is included only when `ls(path, long=True)` is called, which performs a batched `VersionMeta` lookup (`size` lives on `VersionMeta`, not `FileMeta`).
  Result count is capped by `resource_limits.max_result_items` with a truncation flag when exceeded.
- `write(path, data)` → `session.write(path, data)`; returns `{"version_number": int, "size": int}` (a plain marshalable dict, not the raw `VersionMeta` model).

**Budget independence:** `grep` and `find` each count as ONE operation against `ResourceLimits.max_operations`.
Their internal blob I/O is governed exclusively by the search layer's own `SearchLimits` budget; it does not draw from `max_read_bytes`.
`max_read_bytes` governs only direct content reads (`cat`/`head`/`tail`), not search-internal reads.

Serves: monty-code-mode

#### Scenario: GrepDispatchesToSearch

- **GIVEN** a session-backed `FsOperations` with `NativeTextSearch` active
- **WHEN** `grep(pattern, path)` is called
- **THEN** `session.search` is invoked with `search_type=REGEX` and matching lines are returned

#### Scenario: GrepPropagatesColdIndex

- **GIVEN** the active metadata store has a cold (unindexed) search index
- **WHEN** `grep(pattern, path)` is called
- **THEN** `ReindexRequiredError` propagates out of `grep` unchanged (not swallowed)

#### Scenario: FindWithPredicates

- **GIVEN** an `FsOperations` instance with files of varying names and sizes
- **WHEN** `find(path, name="*.py", size_max=10000)` is called
- **THEN** only `.py` files under the size limit are returned

#### Scenario: GlobPatternMatch

- **GIVEN** a directory containing `a.py`, `b.py`, and `c.txt`
- **WHEN** `glob("*.py")` is called
- **THEN** only `a.py` and `b.py` are returned

#### Scenario: LsStructuredOutput

- **GIVEN** a directory with known entries
- **WHEN** `ls(path)` is called
- **THEN** each entry is a dict containing `name`, `path`, `is_dir`, `version_number`, and
  `updated_at`; `size` is absent

#### Scenario: LsLongIncludesSize

- **GIVEN** a directory with known entries
- **WHEN** `ls(path, long=True)` is called
- **THEN** each entry additionally contains `size` (from a batched `VersionMeta` lookup)

#### Scenario: HeadTailSlice

- **GIVEN** a file with 20 lines
- **WHEN** `head(path, 5)` is called
- **THEN** the first 5 lines are returned
- **WHEN** `tail(path, 5)` is called
- **THEN** the last 5 lines are returned

#### Scenario: OversizedReadReturnsError

- **GIVEN** a file whose content size exceeds `resource_limits.max_read_bytes`
- **WHEN** `cat(path)` is called
- **THEN** a structured error is returned; the host does not OOM

#### Scenario: BinaryFileReturnsError

- **GIVEN** a file whose content is not valid UTF-8
- **WHEN** `cat(path)` is called
- **THEN** a structured error is returned

### Requirement: FsOperationsFactory

The system SHALL provide an `FsOperations` dataclass whose fields are async callables corresponding to the ten shell wrappers (`cd`, `pwd`, `cat`, `head`, `tail`, `ls`, `grep`, `find`, `glob`, `write`) plus internal fields (`read`, `stat`, `delete`) for use within the execution layer.
The system SHALL provide a `fs_operations_for(session, resource_limits)` factory that constructs all wrappers bound to the session, wires the shared `OperationCounter`, and returns an `FsOperations` instance.
All shell wrappers except `pwd` and `cd` SHALL resolve relative paths through `session.cwd` before invoking the underlying VFS operation.

Each invocation of any shell wrapper SHALL increment a shared `OperationCounter`.
When the counter reaches `resource_limits.max_operations`, the next call SHALL raise `OperationBudgetExceededError` before invoking the underlying VFS operation.
The counter SHALL be scoped to a single `FsOperations` instance; separate calls to `fs_operations_for` produce independent counters.

Serves: monty-code-mode

#### Scenario: RelativePathResolved

- **GIVEN** an `FsOperations` instance bound to a session with `cwd="/src/"`
- **WHEN** `cat("utils.py")` is called
- **THEN** the session receives `read("/src/utils.py")`

#### Scenario: BudgetExceededOnOverflow

- **GIVEN** an `FsOperations` instance with `max_operations=1000`
- **WHEN** the 1001st shell wrapper call is made
- **THEN** `OperationBudgetExceededError` is raised and the underlying VFS operation is not invoked

#### Scenario: CounterFreshPerExecution

- **GIVEN** two separate `FsOperations` instances each with `max_operations=10`, both having
  exhausted their budgets
- **WHEN** each is inspected independently
- **THEN** calls against one instance do not affect the counter of the other

### Requirement: VfsExecutePermission

The system SHALL provide `vfs.execute(code, namespace_id, principal_id, provider_name, timeout, resource_limits, cwd="/")`.
`cwd` must be a canonical path and defaults to `"/"`.
`vfs.execute` uses a two-tier error contract:

**Tier 1 — raises for caller-side errors (before dispatch):**

- `ValueError` for malformed arguments (non-canonical `cwd`) or unknown provider name.
- `PermissionDeniedError` if the principal does not have `execute` permission on `cwd`
  (consistent with every other VFS operation; no session or FsOperations is constructed).

**Tier 2 — returns `ExecutionResult(success=False, ...)` for errors arising during execution.**

If the caller-side checks pass, `vfs.execute` SHALL construct a `Session` bound to `cwd` via `session.cd(cwd)` (which also enforces read permission on `cwd`), construct an `FsOperations` and the FS-port the provider mounts, resolve the named provider via `resolve_execution_provider`, and dispatch to the provider's `execute` method wrapped in `asyncio.wait_for(..., timeout=timeout)`.
Sandbox filesystem access is NOT confined to the execute scope (`cwd`); it is governed by the principal's normal read/write/delete permissions.
The `execute` permission gates entry at a scope; per-operation permissions gate every FS call inside.

Serves: monty-code-mode, governed-mount

#### Scenario: ExecuteRequiresPermission

- **GIVEN** a principal with no `execute` permission on the namespace
- **WHEN** `vfs.execute(code, namespace_id, principal_id, ...)` is called
- **THEN** `PermissionDeniedError` is raised and no session, `FsOperations`, or provider is constructed

#### Scenario: ExecuteGrantedAllows

> Requires the `monty` extra; mark `pytest.mark.skipif(not HAS_MONTY, ...)`.

- **GIVEN** a principal with `execute` permission on `/workspace/` and `cwd="/workspace/"`
- **WHEN** `vfs.execute` is called with `MontyExecutionProvider` and a simple expression (`"1 + 1"`)
- **THEN** `ExecutionResult(success=True, output=2)` is returned

#### Scenario: ExecuteCwdDenied

- **GIVEN** a principal with `execute` permission on `/workspace/` only
- **WHEN** `vfs.execute(code, ..., cwd="/")` is called (cwd not covered by the grant)
- **THEN** `PermissionDeniedError` is raised before any session or FsOperations is constructed

### Requirement: MontyProviderIntegration

> Previously: `execute` passed only the async `FsOperations` callables as
> `external_functions`; there was no native filesystem mount.
>
> **Note:** All scenarios in this requirement depend on the `monty` optional extra
> (`pydantic-monty>=0.0.18,<0.1`). Tests are marked
> `pytest.mark.skipif(not HAS_MONTY, reason="pydantic-monty not installed")`.

The system SHALL provide `MontyExecutionProvider` as an optional execution provider behind the `monty` extra.
Its `execute` method SHALL run the sandboxed code with both surfaces wired: the async `FsOperations` callables passed as `external_functions` (the injected verbs, kept additively — `grep`/`find`/`glob` and the file-I/O verbs), and the FS-port mounted as the sandbox's native filesystem (see `MontyNativeFilesystemMount`). pydantic-monty awaits coroutine-returning external functions on the host event loop, so no thread bridging is used for `external_functions`; the native filesystem mount uses the FS-port bridge.
The provider SHALL resolve directly to the output value and construct `ExecutionResult` from it.
VFS `ResourceLimits` SHALL be mapped onto pydantic-monty's `ResourceLimits`: `timeout_seconds` → `max_duration_secs`; `max_memory_bytes` → `max_memory`.
Field names are verified against the installed package at integration time; unmapped fields are documented as unenforced at the provider level.
Monty-internal errors (sandbox timeout, memory limit, syntax error) SHALL be mapped to `ExecutionResult(success=False, error_type="provider_error", ...)` with no host path in `error_message`.

Serves: monty-code-mode

#### Scenario: SimpleExpressionReturnsOutput

- **GIVEN** `MontyExecutionProvider` is instantiated and `pydantic-monty` is installed
- **WHEN** `vfs.execute("1 + 2", ...)` is called
- **THEN** `ExecutionResult(success=True, output=3)` is returned

#### Scenario: NativeFilesystemAccessFromSandbox

- **GIVEN** a file exists in the VFS and the principal has read permission
- **WHEN** Monty sandbox code reads it via native `open`/`pathlib` and writes it back via
  native `open(path, "w").write(...)` (no injected verb)
- **THEN** the VFS content is returned and the native write persists a new version,
  demonstrating the mount is wired alongside `external_functions`

#### Scenario: GrepBridgesAsyncSearch

- **GIVEN** a session with files indexed for native text search
- **WHEN** Monty sandbox code calls `grep(pattern, path)` via its `external_functions`
- **THEN** the call reaches `session.search` as a coroutine awaited on the host event loop and
  returns results

#### Scenario: MontyInternalTimeoutProducesProviderError

- **GIVEN** Monty sandbox code that exceeds Monty's own inner duration limit
- **WHEN** `MontyExecutionProvider.execute` receives the timeout result from Monty
- **THEN** `ExecutionResult(success=False, error_type="provider_error")` is returned;
  `error_message` contains no host path

#### Scenario: EventLoopHeartbeatDuringExecution

- **GIVEN** a concurrent `asyncio.Task` that records ticks at a regular interval
- **WHEN** `vfs.execute` runs a compute-heavy sandbox script via `MontyExecutionProvider`
- **THEN** the heartbeat task continues ticking throughout execution (event loop is not starved)

### Requirement: ExecutionProviderRegistry

> Previously: only `MontyExecutionProvider` was registered, behind the `monty` extra; the
> requirement stated no built-in provider existed beyond `monty`.

The system SHALL provide `resolve_execution_provider(name, config)` that maps a provider name string to an `ExecutionProvider` instance, using lazy imports following the same pattern as the metadata and blob resolver factories.
When a provider name is unknown, `resolve_execution_provider` SHALL raise with a clear actionable message (e.g. "Unknown provider 'X'").
When a provider requires an optional extra that is not installed, `resolve_execution_provider` SHALL raise with a clear "install ai-vfs[extra]" message naming that provider's extra, rather than an import-error traceback.
`MontyExecutionProvider` SHALL be registered behind the `monty` extra and the just-bash provider behind the `just-bash` extra; the `codemode` umbrella extra installs both.
The VFS layer SHALL import and operate with no execution provider installed; providers are resolved only on demand.

Serves: portable-sandboxes, just-bash-shell-tool

#### Scenario: UnknownProviderRejected

- **GIVEN** `resolve_execution_provider("nonexistent", config)` is called
- **WHEN** the factory looks up the name
- **THEN** an error is raised with a message identifying the unknown name before any
  `FsOperations` or session is constructed

#### Scenario: MissingMontyExtraRaises

- **GIVEN** the `pydantic-monty` package is not installed
- **WHEN** `resolve_execution_provider("monty", config)` is called
- **THEN** an error is raised instructing the caller to run `pip install ai-vfs[monty]` (or
  equivalent); no `ImportError` traceback is exposed

#### Scenario: MissingJustBashExtraRaises

- **GIVEN** the `just-bash` package is not installed
- **WHEN** `resolve_execution_provider("just-bash", config)` is called
- **THEN** an error is raised instructing the caller to run `pip install ai-vfs[just-bash]` (or
  equivalent); no `ImportError` traceback is exposed

#### Scenario: VfsImportsWithoutAnyProvider

- **GIVEN** neither execution extra is installed
- **WHEN** the `vfs` package is imported and a non-execute VFS operation is performed
- **THEN** it succeeds; the absence of any execution provider does not break the VFS layer

## REMOVED Requirements

### Requirement: AnchoredEditing

Removed because: hash-anchored editing is deferred to a future change at `.specs/changes/2026-06-30-anchored-editing/`, whose design space is unresolved.
This change removes the baseline anchored-edit surface — the per-`execute` `AnchorMap` (token pool, stored `(path, version_number, line_index, line_content)` entries, difflib reconciliation, invalidate-on-write), the `edit` verb, and the out-of-band anchors on `cat`/`head`/`tail`.
Code-mode editing is done with native file I/O through the mount (see the modified `ShellOperationsLayer` and `MontyNativeFilesystemMount`).
