# Execution — Spec

## Requirements

### Requirement: ExecutionProtocol

The system SHALL define an `ExecutionProvider` protocol with `execute`, `capabilities`, and
`reset` methods.
`execute(code, fs_ops, resource_limits)` SHALL be an `async def` accepting a code string,
an `FsOperations` instance, and a `ResourceLimits` value, and SHALL return an `ExecutionResult`.
(End-to-end timeout is enforced by `vfs.execute` via `asyncio.wait_for`; providers may use
`resource_limits.timeout_seconds` as a secondary inner limit.)
`capabilities()` SHALL return an `ExecutionCapabilities` value describing the provider's language,
tier, and whether it supports async execution.
`reset()` SHALL return `None` and perform any provider-level state reset.

`ExecutionResult` SHALL be a frozen dataclass with fields `success: bool`, `output: Any`,
`error_type: str | None`, and `error_message: str | None`.
`ExecutionCapabilities` SHALL be a frozen dataclass with fields `supports_async: bool`,
`language: str`, and `tier: str`.
`ResourceLimits` SHALL be a dataclass with fields `timeout_seconds: float`,
`max_memory_bytes: int | None`, `max_operations: int`, `max_read_bytes: int | None`, and
`max_result_items: int | None`, with defaults `timeout_seconds=30.0` and `max_operations=1000`.
`max_read_bytes` caps the content size returned by a single `cat`/`head`/`tail`/read call;
an oversized file yields a structured error rather than causing a host OOM.
`max_result_items` caps the number of items returned by `grep`/`find`/`ls`; truncation is
flagged on the result object.

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

### Requirement: FsOperationsFactory

The system SHALL provide an `FsOperations` dataclass whose fields are async callables corresponding to the eleven shell wrappers (`cd`, `pwd`, `cat`, `head`, `tail`, `ls`, `grep`, `find`, `glob`, `write`, `edit`) plus internal fields (`read`, `stat`, `delete`) for use within the execution layer.
The system SHALL provide a `fs_operations_for(session, resource_limits, anchor_map)` factory that constructs all wrappers bound to the session, wires the shared `OperationCounter`, and returns an `FsOperations` instance.
All shell wrappers except `pwd` and `cd` SHALL resolve relative paths through `session.cwd` before invoking the underlying VFS operation.

Each invocation of any shell wrapper SHALL increment a shared `OperationCounter`.
When the counter reaches `resource_limits.max_operations`, the next call SHALL raise `OperationBudgetExceededError` before invoking the underlying VFS operation.
The counter SHALL be scoped to a single `FsOperations` instance; separate calls to `fs_operations_for` produce independent counters.

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

### Requirement: ShellOperationsLayer

The `FsOperations` shell wrappers SHALL implement the following dispatch:

- `grep(pattern, path, recursive=True)` → `session.search(query=pattern, scope=resolved_path, search_type=SearchType.REGEX)`, returning structured match dicts; re-raises `ReadBudgetExceededError`, `ReindexRequiredError`, and `IndexUnavailableError` unchanged.
  When `recursive=False`, results are post-filtered to depth-1 files only (files whose path has no additional `/` segment after the resolved scope); the underlying `session.search` always scans recursively.
- `find(path, **predicates)` → `session.search(scope=path, search_type=FIND, find_predicates=FindPredicates(**predicates))`.
- `glob(pattern)` → `session.search` with `search_type=GLOB`.
- `cat(path)` → `session.read(path)` decoded as strict UTF-8; undecodable content yields a structured error and no anchors.
  Content split on `\n` only (`\r` kept in line content); trailing-newline presence preserved.
  `anchor_map.allocate` on all lines; returns content with a separate `anchors` dict mapping `line_index → anchor_token` (not inline tokens in the line text).
  A path beginning with `//` is accepted as a canonical absolute path by the POSIX path resolver and is permission-checked like any other absolute path.
  Raises a structured error (not host OOM) if content exceeds `resource_limits.max_read_bytes`; the size check is performed via `stat` before the blob is fetched when `max_read_bytes` is set.
- `head(path, n)` / `tail(path, n)` → same UTF-8 decode and line model as `cat`; line slicing applied before anchor allocation; `anchor_map.allocate` on the sliced lines only.
  For `tail`, anchor `line_index` values are file-absolute (offset from the start of the full file, not the slice).
- `ls(path)` → `session.list(path)` mapped to a list of dicts with fields `name`, `path`, `is_dir` (synthesized for implicit directory prefixes via an internal recursive scan), `version_number`, and `updated_at`.
  `size` is included only when `ls(path, long=True)` is called, which performs a batched `VersionMeta` lookup (`size` lives on `VersionMeta`, not `FileMeta`).
  Result count is capped by `resource_limits.max_result_items` with a truncation flag when exceeded.
- `write(path, data)` → `session.write(path, data)` followed by `anchor_map.invalidate(path)` on success; returns `{"version_number": int, "size": int}` (a plain marshalable dict, not the raw `VersionMeta` model).
- `edit(path, start_anchor, end_anchor, replacement, expected_version=None)` → anchor validation then `session.write`; see `AnchoredEditing` requirement.

**Budget independence:** `grep` and `find` each count as ONE operation against `ResourceLimits.max_operations`.
Their internal blob I/O is governed exclusively by the search layer's own `SearchLimits` budget (per the design's budget-independence decision); it does not draw from `max_read_bytes`.
`max_read_bytes` governs only direct content reads (`cat`/`head`/`tail`), not search-internal reads.

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
- **THEN** each entry is a dict containing `name`, `path`, `is_dir`, `version_number`, and `updated_at`; `size` is absent

#### Scenario: LsLongIncludesSize

- **GIVEN** a directory with known entries
- **WHEN** `ls(path, long=True)` is called
- **THEN** each entry additionally contains `size` (from a batched `VersionMeta` lookup)

#### Scenario: HeadTailSlice

- **GIVEN** a file with 20 lines
- **WHEN** `head(path, 5)` is called
- **THEN** the first 5 lines are returned (with anchors for those 5 lines only)
- **WHEN** `tail(path, 5)` is called
- **THEN** the last 5 lines are returned (with anchors for those 5 lines only)

#### Scenario: OversizedReadReturnsError

- **GIVEN** a file whose content size exceeds `resource_limits.max_read_bytes`
- **WHEN** `cat(path)` is called
- **THEN** a structured error is returned and no anchors are emitted; the host does not OOM

#### Scenario: BinaryFileReturnsError

- **GIVEN** a file whose content is not valid UTF-8
- **WHEN** `cat(path)` is called
- **THEN** a structured error is returned and no anchors are emitted

#### Scenario: WriteInvalidatesAnchors

- **GIVEN** anchors have been allocated for a path via `cat`
- **WHEN** `write(path, new_content)` is called through `FsOperations`
- **THEN** `anchor_map.invalidate(path)` is called and subsequent `validate` calls for that path's old anchors raise `AnchorConflictError`

### Requirement: AnchoredEditing

The system SHALL provide an `AnchorMap` object, constructed inside `fs_operations_for` and closed over by the shell wrappers.
Its lifetime SHALL match the `FsOperations` instance (one `execute` call).
Anchors SHALL be allocated from a fixed single-token pool on first use; when the pool is exhausted the allocator SHALL fall back to short (2–4 character) random strings that do not collide with pool entries.
Each anchor entry SHALL bind `(path, version_number, line_index, line_content)`.
Validation: resolve the anchor, check the file's current version equals the recorded `version_number`, then check that the line at `line_index` in the current content equals `line_content`.
Anchored operations (`cat`/`head`/`tail`, `edit`) SHALL decode content as strict UTF-8; undecodable content SHALL yield a structured error and no anchors.
Line model: content is split on `\n` only; `\r` is kept as part of line content; the presence or absence of a trailing newline is preserved through `edit()`.

`edit(path, start_anchor, end_anchor, replacement, expected_version=None)` SHALL proceed in two stages:

1. **Anchor validation (pre-write):** Resolve start/end anchors; if the file's current version (via `session.stat`) differs from the anchor's recorded version, raise `AnchorConflictError`.
   Then verify that the line at the stored `line_index` still equals the recorded `line_content`; if not, raise `AnchorConflictError`.
   If the caller supplies `expected_version` and it differs from the anchor's recorded `version_number`, raise `AnchorConflictError` immediately, before any write.
2. **Write:** Construct replacement content; `edit()` SHALL always pass the anchor-validated `version_number` as `expected_version` to `session.write`.
   If `session.write` raises `ConflictError` or `VersionCollisionError`, surface it as `AnchorConflictError` — never retried.
   On success, the path's anchor state is atomically REPLACED with the longest-common-block (`difflib.SequenceMatcher`) reconciliation result (no prior `invalidate` call).

`reconcile` SHALL run a longest-common-block diff (via `difflib.SequenceMatcher`); unchanged lines SHALL keep their existing anchor tokens with updated `line_index` and the new `version_number`; changed or inserted lines SHALL receive new tokens from the pool; dropped lines SHALL be removed from the map.
A raw `write()` or `delete()` through `FsOperations` SHALL call `anchor_map.invalidate(path)`; a successful `edit()` SHALL call `anchor_map.reconcile(...)` without a prior invalidation.

#### Scenario: SingleTokenPoolFirst

- **GIVEN** a fresh `AnchorMap` and a 5-line file
- **WHEN** anchors are allocated for those 5 lines
- **THEN** the first allocations use entries from the single-token pool (not multi-character fallback strings)

#### Scenario: ValidateKnownAnchor

- **GIVEN** an anchor allocated for line 3 of `/src/a.py` at version 2 with content `"  return x"`
- **WHEN** `validate(anchor_token, "/src/a.py")` is called
- **THEN** `(2, "  return x")` is returned

#### Scenario: ValidateWrongPathConflict

- **GIVEN** an anchor allocated for `/src/a.py`
- **WHEN** `validate(anchor_token, "/src/b.py")` is called (different path)
- **THEN** `AnchorConflictError` is raised

#### Scenario: StaleVersionConflict

- **GIVEN** an anchor allocated at version 2 of `/src/a.py`
- **WHEN** the file has since been written to version 3 and `edit()` is called using the version-2 anchor
- **THEN** `AnchorConflictError` is raised during the stat pre-check before any write is attempted

#### Scenario: StaleLineContentConflict

- **GIVEN** an anchor allocated for a line with content `"  return x"` at the current version
- **WHEN** a concurrent edit changes that line's content (same version, shifted line)
- **THEN** `AnchorConflictError` is raised during the line content check

#### Scenario: SuccessfulEditReturnsUpdatedAnchors

- **GIVEN** a file with 10 lines and anchors for all lines
- **WHEN** `edit()` replaces lines 4–6 with 2 new lines
- **THEN** the write succeeds, the result carries updated anchors, lines 1–3 and 7–10 keep their
  original anchor tokens, and lines 4–5 (the replaced range) have new tokens

#### Scenario: CasConflictSurfacesAsAnchorConflict

- **GIVEN** `session.write` raises `ConflictError` (CAS mismatch on `expected_version`)
- **WHEN** `edit()` propagates the error
- **THEN** the caller receives `AnchorConflictError` (not a raw `ConflictError`)

#### Scenario: InvalidatedAnchorRejected

- **GIVEN** anchors have been allocated for `/src/a.py`
- **WHEN** a raw `write(path, ...)` through `FsOperations` calls `anchor_map.invalidate("/src/a.py")`
- **THEN** a subsequent `validate` call for any of that path's old anchor tokens raises `AnchorConflictError`

#### Scenario: EditReconcilesAnchorsAtomically

- **GIVEN** a file with anchors allocated for all lines
- **WHEN** `edit()` succeeds and replaces lines 3–5
- **THEN** the path's anchor state is replaced atomically (reconcile, no prior invalidate);
  unchanged-line anchors remain valid with updated `line_index` and the new `version_number`

#### Scenario: DifflibReconcilePreservesUnchangedAnchors

- **GIVEN** a 10-line file with anchors allocated for all lines
- **WHEN** `reconcile` is called with lines 4–6 replaced by 2 new lines
- **THEN** anchors for lines 1–3 and 7–10 are preserved (same tokens), and lines 4–5 have new tokens

### Requirement: VfsExecutePermission

The system SHALL provide `vfs.execute(code, namespace_id, principal_id, provider_name, timeout, resource_limits, cwd="/")`.
`cwd` must be a canonical path and defaults to `"/"`.
`vfs.execute` uses a two-tier error contract:

**Tier 1 — raises for caller-side errors (before dispatch):**

- `ValueError` for malformed arguments (non-canonical `cwd`) or unknown provider name.
- `PermissionDeniedError` if the principal does not have `execute` permission on `cwd`
  (consistent with every other VFS operation; no session or FsOperations is constructed).

**Tier 2 — returns `ExecutionResult(success=False, ...)` for errors arising during execution.**

If the caller-side checks pass, `vfs.execute` SHALL construct a `Session` bound to `cwd` via
`session.cd(cwd)` (which also enforces read permission on `cwd`), construct an `AnchorMap` and
`FsOperations`, resolve the named provider via `resolve_execution_provider`, and dispatch to
the provider's `execute` method wrapped in `asyncio.wait_for(..., timeout=timeout)`.

Sandbox filesystem access is NOT confined to the execute scope (`cwd`); it is governed by the principal's normal read/write/delete permissions.
The `execute` permission gates entry at a scope; per-operation permissions gate every FS call inside.

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

### Requirement: VfsExecuteErrorTranslation

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
| `AnchorConflictError`          | `"anchor_conflict"`    |
| `ReadBudgetExceededError`      | `"search_unavailable"` |
| `ReindexRequiredError`         | `"search_unavailable"` |
| `IndexUnavailableError`        | `"search_unavailable"` |
| Unexpected `Exception`         | `"internal_error"`     |

`vfs.execute` wraps provider dispatch in `asyncio.wait_for(..., timeout=resource_limits.timeout_seconds)`;
on expiry the provider task is cancelled and `ExecutionResult(success=False, error_type="timeout")` is returned.

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

### Requirement: ExecutionProviderRegistry

The system SHALL provide `resolve_execution_provider(name, config)` that maps a provider name string to an `ExecutionProvider` instance, using lazy imports following the same pattern as the metadata and blob resolver factories.
When a provider name is unknown, `resolve_execution_provider` SHALL raise with a clear actionable message (e.g. "Unknown provider 'X'").
When a provider requires an optional extra that is not installed, `resolve_execution_provider` SHALL raise with a clear "install ai-vfs[extra]" message rather than an import-error traceback.
`MontyExecutionProvider` SHALL be registered behind the `monty` extra.
Unknown provider names SHALL raise with a clear actionable message; no built-in provider is registered for this change beyond `monty`.

#### Scenario: UnknownProviderRejected

- **GIVEN** `resolve_execution_provider("nonexistent", config)` is called
- **WHEN** the factory looks up the name
- **THEN** an error is raised with a message identifying the unknown name before any
  `FsOperations` or session is constructed

#### Scenario: MissingMontyExtraRaises

- **GIVEN** the `pydantic-monty` package is not installed
- **WHEN** `resolve_execution_provider("monty", config)` is called
- **THEN** an error is raised with a message instructing the caller to run
  `pip install ai-vfs[monty]` (or equivalent); no `ImportError` traceback is exposed

### Requirement: MontyProviderIntegration

> **Note:** All scenarios in this requirement depend on the `monty` optional extra
> (`pydantic-monty>=0.0.18,<0.1`). Tests are marked
> `pytest.mark.skipif(not HAS_MONTY, reason="pydantic-monty not installed")`.
> They are normal unit tests that skip automatically without the extra; they run in dev with
> `uv sync --extra monty`.

The system SHALL provide `MontyExecutionProvider` as an optional execution provider behind the `monty` extra.
Its `execute` method SHALL await `monty.run_async(...)`, passing the async `FsOperations` callables directly in `external_functions`; pydantic-monty awaits coroutine-returning external functions on the host event loop, so no thread bridging is used.
`run_async` resolves directly to the output value; `ExecutionResult` is constructed from that value.
VFS `ResourceLimits` SHALL be mapped onto pydantic-monty's `ResourceLimits` (imported aliased as `MontyResourceLimits` to avoid the name collision): `timeout_seconds` → `max_duration_secs`; `max_memory_bytes` → `max_memory`.
Field names are verified against the installed stub at integration time; unmapped fields are documented as unenforced at the provider level.
Monty-internal errors (sandbox timeout, memory limit, syntax error) SHALL be mapped to `ExecutionResult(success=False, error_type="provider_error", ...)` with no host path in `error_message`.

#### Scenario: SimpleExpressionReturnsOutput

- **GIVEN** `MontyExecutionProvider` is instantiated and `pydantic-monty` is installed
- **WHEN** `vfs.execute("1 + 2", ...)` is called
- **THEN** `ExecutionResult(success=True, output=3)` is returned

#### Scenario: GrepBridgesAsyncSearch

- **GIVEN** a session with files indexed for native text search
- **WHEN** Monty sandbox code calls `grep(pattern, path)` via its `external_functions`
- **THEN** the call reaches `session.search` as a coroutine awaited on the host event loop and returns results

#### Scenario: MontyInternalTimeoutProducesProviderError

- **GIVEN** Monty sandbox code that exceeds Monty's own `max_duration_secs` inner limit
- **WHEN** `MontyExecutionProvider.execute` receives the timeout result from Monty
- **THEN** `ExecutionResult(success=False, error_type="provider_error")` is returned; `error_message` contains no host path

#### Scenario: EventLoopHeartbeatDuringExecution

- **GIVEN** a concurrent `asyncio.Task` that records ticks at a regular interval
- **WHEN** `vfs.execute` runs a compute-heavy sandbox script via `MontyExecutionProvider`
- **THEN** the heartbeat task continues ticking throughout execution (event loop is not starved)

#### Scenario: EditFromSandboxWorks

- **GIVEN** a file exists in the VFS and the sandbox has `execute` and `write` permissions
- **WHEN** Monty sandbox code calls `edit(path, start_anchor, end_anchor, replacement)`
- **THEN** the file is modified in the VFS and updated anchors are returned to the sandbox
