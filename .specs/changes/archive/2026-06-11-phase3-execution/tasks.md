# Tasks: Phase 3 (Execution)

## Protocol + Models

- [x] Define `ExecutionProvider` protocol (`execute`, `capabilities`, `reset`) in `src/vfs/protocols/execution.py`
- [x] Define `ExecutionResult` frozen dataclass (`success`, `output`, `error_type`, `error_message`)
- [x] Define `ExecutionCapabilities` frozen dataclass (`supports_async`, `language`, `tier`)
- [x] Extend `ResourceLimits` dataclass (`timeout_seconds`, `max_memory_bytes`, `max_operations`, `max_read_bytes`, `max_result_items`) — move from design-doc sketch to `src/vfs/models.py` or `src/vfs/protocols/execution.py`
- [x] Add `OperationBudgetExceededError` and `AnchorConflictError` to `src/vfs/errors.py`
- [x] Test: `ExecutionResult(success=True, output=42)` round-trips through the dataclass with `error_type` and `error_message` defaulting to `None` (`ExecutionProtocol`/`ExecutionResultFields`)
- [x] Test: `ExecutionResult(success=False, error_type="conflict", error_message="...")` round-trips with all failure fields populated (`ExecutionProtocol`/`ExecutionResultFailureFields`)
- [x] Test: `ResourceLimits` defaults match §8.3 (`max_operations=1000`, `timeout_seconds=30.0`) (`ExecutionProtocol`/`ResourceLimitsDefaults`)

## FsOperations + Rate Limiting

- [x] Implement `FsOperations` dataclass in `src/vfs/execution/fs_ops.py` with all ten shell fields: `cd`, `pwd`, `cat`, `head`, `tail`, `ls`, `grep`, `find`, `glob`, `write`, `edit` (plus raw `read`, `stat`, `delete` for internal use)
- [x] Implement `OperationCounter` wrapper: shared counter across all `FsOperations` callables; raises `OperationBudgetExceededError` at `max_operations`
- [x] Implement `fs_operations_for(session, resource_limits, anchor_map)` factory: constructs all wrappers, wires `OperationCounter`, returns `FsOperations`
- [x] Wire `grep` → `session.search(type=REGEX)`; re-raise `ReadBudgetExceededError`, `ReindexRequiredError`, `IndexUnavailableError` unchanged
- [x] Wire `find` → `session.search(type=FIND, find_predicates=FindPredicates(**predicates))` — passes through `find_predicates` parameter added to `Session.search`
- [x] Wire `glob` → `session.search(type=GLOB)`
- [x] Wire `cat` → `session.read` + strict UTF-8 decode (undecodable → structured error, no anchors) + `\n`-split line model + `anchor_map.allocate` (all lines); enforce `max_read_bytes` cap
- [x] Wire `head`/`tail` → same UTF-8 decode and line model as `cat`; slice before anchor allocation; `anchor_map.allocate` on sliced lines only
- [x] Wire `ls` → `session.list` → structured list of dicts with fields `name`, `path`, `is_dir`, `version_number`, `updated_at`; `size` added only on `ls(long=True)` via batched `VersionMeta` lookup; result count capped by `max_result_items`
- [x] Wire `write` → `session.write` + `anchor_map.invalidate(path)` after successful write
- [x] Test: `cat` on a relative path resolves to the absolute path via `session.cwd`; call `FsOperations.cat("utils.py")` directly against a `Session` with `cwd="/src/"` and verify `session.read("/src/utils.py")` is invoked (`FsOperationsFactory`/`RelativePathResolved`)
- [x] Test: 1001st callback raises `OperationBudgetExceededError` when `max_operations=1000` (`FsOperationsRateLimiting`/`BudgetExceededOnOverflow`)
- [x] Test: counter resets between separate `fs_operations_for` calls (each `execute` starts fresh) (`FsOperationsRateLimiting`/`CounterFreshPerExecution`)
- [x] Test: `grep` on a store with `NativeTextSearch` returns matching results (`ShellOperationsLayer`/`GrepDispatchesToSearch`)
- [x] Test: `grep` on a cold index propagates `ReindexRequiredError` unchanged — not swallowed, not wrapped (`ShellOperationsLayer`/`GrepPropagatesColdIndex`)
- [x] Test: `find` with name + size predicates returns only matching files (`ShellOperationsLayer`/`FindWithPredicates`)
- [x] Test: `glob` with `*.py` pattern returns only `.py` files (`ShellOperationsLayer`/`GlobPatternMatch`)
- [x] Test: `ls` returns structured dicts with `name`, `path`, `is_dir`, `version_number`, `updated_at`; `size` absent by default (`ShellOperationsLayer`/`LsStructuredOutput`)
- [x] Test: `ls(long=True)` additionally includes `size` from batched `VersionMeta` lookup (`ShellOperationsLayer`/`LsLongIncludesSize`)
- [x] Test: `cat` on a file exceeding `max_read_bytes` returns a structured error and no anchors (`ShellOperationsLayer`/`OversizedReadReturnsError`)
- [x] Test: `cat` on a binary (non-UTF-8) file returns a structured error and no anchors (`ShellOperationsLayer`/`BinaryFileReturnsError`)
- [x] Test: `head(path, 5)` returns first 5 lines; `tail(path, 5)` returns last 5 lines (`ShellOperationsLayer`/`HeadTailSlice`)
- [x] Test: `write` through `FsOperations` invalidates the anchor map for that path (`ShellOperationsLayer`/`WriteInvalidatesAnchors`)

## Anchored Editing

- [x] Implement `AnchorMap` in `src/vfs/execution/anchors.py`: single-token pool allocation, multi-char fallback, per-entry `(path, version_number, line_index, line_content)` binding
- [x] Implement `AnchorMap.allocate(path, version_number, lines) -> dict[int, str]`: returns anchor tokens keyed by line index
- [x] Implement `AnchorMap.validate(anchor_token, path) -> tuple[int, str]`: returns `(version_number, line_content)` or raises `AnchorConflictError` if anchor unknown or path mismatch
- [x] Implement `AnchorMap.invalidate(path)`: drops all entries for that path
- [x] Implement `AnchorMap.reconcile(path, old_lines, new_lines, version_number) -> dict[int, str]`: Myers diff; unchanged lines keep their anchors with updated `line_index` and the new `version_number`; changed/inserted lines receive new anchors; dropped lines are removed; atomically replaces the path's anchor state (no prior `invalidate` call)
- [x] Implement `edit(path, start_anchor, end_anchor, replacement, expected_version=None)` shell wrapper: (1) stat-based version pre-check; (2) line-content check at stored `line_index`; (3) if caller supplied `expected_version` differing from anchor's, raise `AnchorConflictError`; (4) always pass anchor-validated version as `expected_version` to `session.write`; (5) surface `ConflictError`/`VersionCollisionError` from write as `AnchorConflictError` (never retry); (6) on success call `anchor_map.reconcile(...)` atomically (no prior `invalidate`)
- [x] Test: allocate anchors for a 5-line file; first allocations use single-token pool entries (`AnchoredEditing`/`SingleTokenPoolFirst`)
- [x] Test: validate returns correct `(version_number, line_content)` for a known anchor (`AnchoredEditing`/`ValidateKnownAnchor`)
- [x] Test: validate raises `AnchorConflictError` for an anchor from a different path (`AnchoredEditing`/`ValidateWrongPathConflict`)
- [x] Test: `edit()` on a file whose version has advanced raises `AnchorConflictError` (stat pre-check) (`AnchoredEditing`/`StaleVersionConflict`)
- [x] Test: `edit()` on a file whose anchor line content has changed raises `AnchorConflictError` (line content check) (`AnchoredEditing`/`StaleLineContentConflict`)
- [x] Test: successful `edit()` writes new content, returns updated anchors for the changed range, and unchanged lines keep their original anchors (Myers-diff reconciliation) (`AnchoredEditing`/`SuccessfulEditReturnsUpdatedAnchors`)
- [x] Test: `edit()` receiving `ConflictError` from `session.write` (CAS mismatch) surfaces as `AnchorConflictError` result (`AnchoredEditing`/`CasConflictSurfacesAsAnchorConflict`)
- [x] Test: after raw `write()` invalidates a path, a subsequent `validate()` for that path's old anchors raises `AnchorConflictError` (`AnchoredEditing`/`InvalidatedAnchorRejected`)
- [x] Test: after `edit()` succeeds, unchanged-line anchors remain valid with updated `line_index` and the new version; no prior invalidation was called (`AnchoredEditing`/`EditReconcilesAnchorsAtomically`)
- [x] Test: `reconcile` over a 10-line file where lines 4–6 are replaced preserves anchors for lines 1–3 and 7–10 and allocates new anchors for lines 4–6 (`AnchoredEditing`/`MyersDiffPreservesUnchangedAnchors`)

## `vfs.execute` + Permission Enforcement

- [x] Add `vfs.execute(code, namespace_id, principal_id, provider_name, timeout, resource_limits, cwd="/")` to `VFS` in `src/vfs/vfs.py`; `cwd` must be canonical (raise `ValueError` if not)
- [x] Enforce `execute` permission check on `cwd` at the top of `vfs.execute` (default-deny; raises `PermissionDeniedError` if not granted — not returned as `ExecutionResult`) — reuse `_check_perm`
- [x] Construct `Session` bound to `cwd` via `session.cd(cwd)`, then `AnchorMap` and `FsOperations` inside `vfs.execute`
- [x] Wrap provider dispatch in `asyncio.wait_for(..., timeout=resource_limits.timeout_seconds)`; on `asyncio.TimeoutError` return `ExecutionResult(success=False, error_type="timeout")`
- [x] Implement `resolve_execution_provider(name, config)` in `src/vfs/execution/registry.py` following the lazy-import + "install extra X" pattern from the metadata/blob resolvers
- [x] Implement the error-translation catch block per `design.md` Decision (f): all VFS exceptions → `ExecutionResult(success=False, ...)`; no raw traceback or host path in `error_message`
- [x] Test: principal without `execute` permission causes `vfs.execute` to raise `PermissionDeniedError` with no session or FsOperations constructed (`VfsExecutePermission`/`ExecuteRequiresPermission`, `AccessControl`/`ExecutePermissionEnforced`)
- [x] Test: principal with `execute` on `/workspace/` but `cwd="/"` causes `PermissionDeniedError` (permission checked on cwd, not hardcoded `/`) (`VfsExecutePermission`/`ExecuteCwdDenied`)
- [x] Test: `execute` permission is storable: admin grants `{execute}` on `/workspace/`, permission persists and is queryable (`AccessControl`/`ExecutePermissionStorable`)
- [x] Test: principal with `execute` permission on `/workspace/` and `cwd="/workspace/"` can call `vfs.execute` with `MontyExecutionProvider`; a simple expression returns a successful `ExecutionResult` (marked `skipif(not HAS_MONTY)`) (`VfsExecutePermission`/`ExecuteGrantedAllows`) — **chunk 4 (Monty adapter)**
- [x] Test: `PermissionDeniedError` from a shell-op inside the sandbox (e.g. read on unauthorized path) is translated to `ExecutionResult` with `error_type="permission_denied"` and no host path in `error_message` (`VfsExecuteErrorTranslation`/`PermissionErrorTranslated`)
- [x] Test: `NotFoundError` translated to `ExecutionResult(error_type="not_found")` (`VfsExecuteErrorTranslation`/`NotFoundErrorTranslated`)
- [x] Test: `asyncio.TimeoutError` from `wait_for` translated to `ExecutionResult(error_type="timeout")` and provider task cancelled (`VfsExecuteErrorTranslation`/`TimeoutReturnsStructuredResult`)
- [x] Test: `OperationBudgetExceededError` translated to `ExecutionResult(error_type="budget_exceeded")` (`VfsExecuteErrorTranslation`/`BudgetExceededTranslated`)
- [x] Test: `ReindexRequiredError` translated to `ExecutionResult(error_type="search_unavailable")` (`VfsExecuteErrorTranslation`/`SearchUnavailableTranslated`)
- [x] Test: unexpected `Exception` translated to `ExecutionResult(error_type="internal_error")` with no traceback or path in `error_message` (`VfsExecuteErrorTranslation`/`UnexpectedExceptionSanitized`)
- [x] Test: `vfs.execute` with an unknown `provider_name` raises `ValueError` before constructing `FsOperations` (`VfsExecuteRegistry`/`UnknownProviderRejected`)

## Monty Adapter + Packaging

> All tests in this group require `pydantic-monty>=0.0.18,<0.1` and are marked
> `pytest.mark.skipif(not HAS_MONTY, reason="pydantic-monty not installed")`.
> They are normal unit tests that skip in bare environments and run in dev (`uv sync --extra monty`).

- [x] Verify against the installed pydantic-monty stub: confirm `ResourceLimits` field names (`max_duration_secs`, `max_memory`) and that CPU-bound interpretation between external calls does not starve the event loop (see `design.md` Decision (e) risk; if it blocks, switch to the documented `start()`/`resume()` fallback)
- [x] Implement `MontyExecutionProvider` in `src/vfs/execution/monty_provider.py`: `execute` awaits `monty.run_async(...)` with async `FsOperations` callables passed directly as `external_functions`; `run_async` resolves directly to the output value; constructs `ExecutionResult(success=True, output=output)`; maps VFS `timeout_seconds` → `MontyResourceLimits(max_duration_secs=...)` and `max_memory_bytes` → `max_memory`; maps Monty-internal errors to `ExecutionResult(success=False, error_type="provider_error", ...)`
- [x] Implement `capabilities()` → `ExecutionCapabilities(supports_async=True, language="python", tier="monty")`
- [x] Implement `reset()` → no-op (stateless per-execution; snapshot/restore is out of scope)
- [x] Register `"monty"` in `resolve_execution_provider` with a lazy import raising a clear "install monty extra" message if absent
- [x] Test (requires `monty` extra): `MontyExecutionProvider` executes `"1 + 2"` and returns `ExecutionResult(success=True, output=3)` (`MontyProviderIntegration`/`SimpleExpressionReturnsOutput`)
- [x] Test (requires `monty` extra): `grep` called from Monty sandbox reaches `session.search` as a coroutine awaited on the host event loop and returns results (`MontyProviderIntegration`/`GrepBridgesAsyncSearch`)
- [x] Test (requires `monty` extra): Monty-internal sandbox timeout produces `ExecutionResult(success=False, error_type="provider_error")` with no host path (`MontyProviderIntegration`/`MontyInternalTimeoutProducesProviderError`)
- [x] Test (requires `monty` extra): `edit()` called from Monty sandbox successfully modifies a file and returns updated anchors (`MontyProviderIntegration`/`EditFromSandboxWorks`)
- [x] Test (requires `monty` extra): a concurrent heartbeat `asyncio.Task` keeps ticking throughout a compute-heavy sandbox execution (event loop not starved) (`MontyProviderIntegration`/`EventLoopHeartbeatDuringExecution`)
- [x] Test: without the `monty` extra installed, `resolve_execution_provider("monty", ...)` raises with a clear "install ai-vfs[monty]" message (no import error traceback) (`ExecutionProviderRegistry`/`MissingMontyExtraRaises`)

## Session Proxy + Session.search

- [x] Add `session.execute(code, provider_name, timeout, resource_limits)` proxy method to `Session` in `src/vfs/session.py`: resolves `namespace_id`, `principal_id`, and current `cwd` from session context and delegates to `vfs.execute(cwd=self.cwd)`
- [x] Add `find_predicates=None` passthrough parameter to `Session.search` in `src/vfs/session.py` (verify current signature is `search(query, scope, search_type)`; add `find_predicates` forwarded to `vfs.search`)
- [x] Test: `session.execute(...)` delegates to `vfs.execute` with the session's `namespace_id`, `principal_id`, and current `cwd` (`SessionProxy`/`SessionExecuteProxiesToVfs`)
- [x] Test: `session.search(..., find_predicates=pred)` forwards `find_predicates` to the underlying search call (`SessionSearch`/`FindPredicatesPassthrough`)

## Packaging

- [x] Export `ExecutionProvider`, `ExecutionResult`, `ExecutionCapabilities`, `ResourceLimits`, `FsOperations`, `fs_operations_for` from `vfs.__init__` / `vfs.__all__` as appropriate public API
- [ ] Update `CHANGELOG.md` under Unreleased: execution layer (protocol, FsOperations, shell ops, anchored editing, `vfs.execute`, `MontyExecutionProvider`); `execute` permission now enforced; `monty` optional extra added; `OperationBudgetExceededError`/`AnchorConflictError` added to error hierarchy
  <!-- user-owned: changelog is maintained by the user per governance practices -->
