# Execution — Delta Spec

> Change: `audit-copy-move-execute`
> Date: 2026-06-27

## MODIFIED Requirements

### Requirement: ExecutionProtocol

> Previously: `execute` was specified as `execute(code, fs_ops, resource_limits)` (three args); `ExecutionCapabilities` had only `supports_async`/`language`/`tier`; `ResourceLimits` had no `max_write_bytes`; and the operation budget and read cap were described as enforced only by the injected `FsOperations` verbs.

The system SHALL define an `ExecutionProvider` protocol with `execute`, `capabilities`, and
`reset` methods.
`execute(code, fs_ops, fs_port, resource_limits)` SHALL be an `async def` accepting a code
string, an `FsOperations` instance (the injected shell verbs), an `FsPort` instance (the
session-backed filesystem a provider mounts as the sandbox's native filesystem), and a
`ResourceLimits` value, and SHALL return an `ExecutionResult`.
(End-to-end timeout is enforced by `vfs.execute` via `asyncio.wait_for`; providers may use
`resource_limits.timeout_seconds` as a secondary inner limit.)
`capabilities()` SHALL return an `ExecutionCapabilities` value describing the provider's language,
tier, whether it supports async execution, and whether it enforces the in-sandbox memory limit.
`reset()` SHALL return `None` and perform any provider-level state reset.

`ExecutionResult` SHALL be a frozen dataclass with fields `success: bool`, `output: Any`, `error_type: str | None`, and `error_message: str | None`.
`ExecutionCapabilities` SHALL be a frozen dataclass with fields `supports_async: bool`, `language: str`, `tier: str`, and `enforces_memory_limit: bool` (default `False`).
`enforces_memory_limit` reports whether the provider honours `max_memory_bytes` inside the sandbox; the remaining limits are enforced uniformly regardless of provider, so memory is the only provider-variable guarantee callers must feature-detect.
`ResourceLimits` SHALL be a dataclass with fields `timeout_seconds: float`, `max_memory_bytes: int | None`, `max_operations: int`, `max_read_bytes: int | None`, `max_write_bytes: int | None`, and `max_result_items: int | None`, with defaults `timeout_seconds=30.0` and `max_operations=1000`.
`max_read_bytes` caps the content size returned by a single direct read (`cat`/`head`/`tail` or a native-mount read), and `max_write_bytes` caps the payload accepted by a single write (both the injected `write` verb and a native-mount write); an oversized read/write is refused (a structured error for the injected verbs, or `ResourceLimitExceededError` for the native-mount surface) rather than causing a host OOM.
`max_result_items` caps the number of items returned by `grep`/`find`/`ls`; truncation is flagged on the result object.
The operation budget (`max_operations`) and the `max_read_bytes`/`max_write_bytes` caps SHALL be enforced across BOTH the injected `FsOperations` verbs AND the `FsPort` native mount via a single shared counter, so native `open`/`pathlib` file I/O is governed identically to the injected verbs.

Serves: bounded-sandbox-resources

#### Scenario: ExecutionResultFields

- **GIVEN** `ExecutionResult(success=True, output=42)` is constructed
- **WHEN** the fields are inspected
- **THEN** `success` is `True`, `output` is `42`, `error_type` is `None`, and `error_message` is `None`

#### Scenario: ExecutionResultFailureFields

- **GIVEN** `ExecutionResult(success=False, error_type="conflict", error_message="Version conflict; re-read and retry")` is constructed
- **WHEN** the fields are inspected
- **THEN** `success` is `False`, `error_type` is `"conflict"`, and `error_message` is non-empty

#### Scenario: ResourceLimitsDefaults

- **GIVEN** `ResourceLimits()` is constructed with no arguments
- **WHEN** the fields are inspected
- **THEN** `timeout_seconds` is `30.0` and `max_operations` is `1000`

#### Scenario: CapabilitiesExposeMemoryEnforcement

- **GIVEN** a memory-capping provider (Monty) and a non-capping provider (just-bash)
- **WHEN** `capabilities()` is inspected on each
- **THEN** `enforces_memory_limit` is `True` for the capping provider and `False` for the non-capping one

### Requirement: FsPortContract

> Previously: the FS-port routed operations through the `Session` for permission enforcement and auditing, but did not enforce `ResourceLimits` — a sandbox using the native mount (`open`/`pathlib`) bypassed the operation budget and the read/write size caps.

The system SHALL define an **FS-port**: an async, path-based filesystem interface, backed by a `Session`, exposing whole-file `read`, `write`, `list`, `stat`, `exists`, and `delete`, plus `mkdir` as a no-op over implicit directories.
Every FS-port operation SHALL route through the bound `Session`, so the principal's permissions are enforced and state-changing operations are audited exactly as for direct VFS calls.
The FS-port SHALL NOT expose the host operating system's filesystem.
A filesystem operation that has no VFS equivalent — symbolic links, permission-mode changes, modification-time changes — SHALL raise an unsupported-operation error rather than silently succeed.
When constructed with a `ResourceLimits` and a shared operation counter (as `vfs.execute` does for a sandboxed run), the FS-port SHALL charge each operation against the counter and SHALL refuse a read whose target exceeds `max_read_bytes` (checked via `stat` before the blob is fetched) or a write whose payload exceeds `max_write_bytes`, raising `ResourceLimitExceededError` — so a sandbox using the native mount is governed identically to the injected verbs.

Serves: bounded-sandbox-resources

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

#### Scenario: FsPortEnforcesResourceLimits

- **GIVEN** an FS-port constructed with `ResourceLimits(max_read_bytes=N)` and a shared counter
- **WHEN** a read targets a file larger than `N` bytes
- **THEN** `ResourceLimitExceededError` is raised before the blob is fetched, and the operation is charged against the shared budget

### Requirement: VfsExecuteErrorTranslation

> Previously: the error-translation table did not include `ResourceLimitExceededError`, and no provider-specific outcome (such as a non-zero bash exit) was described.

`vfs.execute` SHALL translate all VFS exceptions to structured `ExecutionResult` failures.
No raw traceback, host path, or adapter-internal detail SHALL appear in `error_message`.
The translation table is:

| Source                         | `error_type`           |
| ------------------------------ | ---------------------- |
| `asyncio.TimeoutError`         | `"timeout"`            |
| `PermissionDeniedError`        | `"permission_denied"`  |
| `NotFoundError`                | `"not_found"`          |
| `ConflictError`                | `"conflict"`           |
| `VersionCollisionError`        | `"conflict"`           |
| `OperationBudgetExceededError` | `"budget_exceeded"`    |
| `ResourceLimitExceededError`   | `"budget_exceeded"`    |
| `ReadBudgetExceededError`      | `"search_unavailable"` |
| `ReindexRequiredError`         | `"search_unavailable"` |
| `IndexUnavailableError`        | `"search_unavailable"` |
| Unexpected `Exception`         | `"internal_error"`     |

`vfs.execute` wraps provider dispatch in `asyncio.wait_for(..., timeout=resource_limits.timeout_seconds)`;
on expiry the provider task is cancelled and `ExecutionResult(success=False, error_type="timeout")` is returned.

A provider MAY additionally return a provider-specific `error_type` for an outcome that is not a
VFS error — e.g. the just-bash provider returns `error_type="nonzero_exit"` when a script runs to
completion but exits non-zero.

Serves: bounded-sandbox-resources, honest-execution-outcomes

#### Scenario: PermissionErrorTranslated

- **GIVEN** a shell operation inside the sandbox raises `PermissionDeniedError` for an unauthorized path
- **WHEN** `vfs.execute` catches it
- **THEN** `ExecutionResult(success=False, error_type="permission_denied")` is returned; `error_message` does not contain any host path

#### Scenario: NotFoundErrorTranslated

- **GIVEN** a shell operation raises `NotFoundError`
- **WHEN** `vfs.execute` catches it
- **THEN** `ExecutionResult(success=False, error_type="not_found")` is returned

#### Scenario: BudgetExceededTranslated

- **GIVEN** the sandbox exhausts its `max_operations` budget
- **WHEN** `OperationBudgetExceededError` propagates to `vfs.execute`
- **THEN** `ExecutionResult(success=False, error_type="budget_exceeded")` is returned

#### Scenario: ResourceLimitExceededTranslated

- **GIVEN** a sandbox native read/write that exceeds `max_read_bytes`/`max_write_bytes`
- **WHEN** `ResourceLimitExceededError` propagates to `vfs.execute`
- **THEN** `ExecutionResult(success=False, error_type="budget_exceeded")` is returned

#### Scenario: SearchUnavailableTranslated

- **GIVEN** `grep` surfaces `ReindexRequiredError` and it propagates to `vfs.execute`
- **WHEN** `vfs.execute` catches it
- **THEN** `ExecutionResult(success=False, error_type="search_unavailable")` is returned

#### Scenario: TimeoutReturnsStructuredResult

- **GIVEN** a sandbox that exceeds the outer `asyncio.wait_for` timeout
- **WHEN** `vfs.execute` catches `asyncio.TimeoutError`
- **THEN** `ExecutionResult(success=False, error_type="timeout")` is returned and the provider task is cancelled

#### Scenario: UnexpectedExceptionSanitized

- **GIVEN** an unexpected exception (not a known VFS error) is raised inside the sandbox
- **WHEN** `vfs.execute` catches it
- **THEN** `ExecutionResult(success=False, error_type="internal_error")` is returned; `error_message` contains no traceback or host path

### Requirement: JustBashProvider

> Previously: the provider returned `ExecutionResult(success=True, output=stdout)` for every run, discarding the script's `exit_code` and `stderr` — a failing command was reported as a success.

The system SHALL provide a just-bash execution provider that runs bash over the governed VFS by injecting an FS-port-backed filesystem, so that bash builtins (`cat`, `ls`, pipes, redirection) operate on VFS files with the principal's permissions enforced.
The provider SHALL replace the `grep`, `find`, and `glob` builtins so they resolve to the VFS search index — parity with the Monty search verbs — rather than brute-force file enumeration.
Filesystem operations with no VFS equivalent SHALL raise unsupported, consistent with the FS-port.
When a script runs to completion but exits non-zero, the provider SHALL return `ExecutionResult(success=False, error_type="nonzero_exit")` carrying the script's stderr in `error_message` (and stdout in `output`), so a failing command is not reported as a success.

Serves: honest-execution-outcomes

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

#### Scenario: BashNonZeroExitReportsFailure

- **GIVEN** the principal has execute permission
- **WHEN** sandboxed bash runs a script that writes to stderr and exits non-zero
- **THEN** `ExecutionResult(success=False, error_type="nonzero_exit")` is returned with the stderr in `error_message`
