# Phase 3 (Execution): Sandboxed Execution Providers

**Change name:** `phase3-execution` **Date:** 2026-06-09 **Author:** ahgraber + Claude

## Intent

Give ai-vfs a sandboxed execution layer so agents can run code-mode orchestration — iterating over files, grepping, editing, and writing new content — through a curated set of VFS-backed shell operations rather than through raw tool calls.

The initial provider is **Monty** (pydantic-monty, alpha), using the function-injection pattern from §8.2: the host constructs `FsOperations` bound to the caller's session, then passes them to Monty as `external_functions`; sandboxed code never touches storage directly.

The shell operations layer (§3.5) and the anchored editing subsystem (§3.6) are also introduced here, because they are the VFS-callable surface that makes the sandbox useful.

**Prerequisites:** `phase1-core`, `shell-context`, `boundary-hardening`, `phase2-storage`, `phase2-search` (all archived).

## Scope

> Listed in build-dependency order; `design.md` Decisions and `tasks.md` groups follow the same order.

### In Scope

- **Protocol + models** (`ExecutionProvider`, `ExecutionResult`, `ExecutionCapabilities`, `ResourceLimits`): the typed protocol and result types from §3.4; `ResourceLimits.max_operations` is the VFS-level cap on FsOperations callbacks.
- **`FsOperations` dataclass** and the `fs_operations_for(session)` factory: bind a `Session` (cwd, namespace, principal) to a set of async callables, then wrap them with a counting enforcement layer that raises `OperationBudgetExceededError` after `max_operations` VFS callbacks.
- **Shell Operations Layer** — the ten bash-familiar wrappers from the §3.5 table (`cd`, `pwd`, `grep`, `find`, `glob`, `cat`, `ls`, `head`, `tail`, `edit`) constructed inside `fs_operations_for`. `grep` dispatches through `session.search`; `find`/`glob` through the metadata path; `cat`/`head`/`tail`/`ls` through `session.read`/`session.list`; `edit` through the anchored edit subsystem (below).
- **Anchored editing subsystem** (§3.6): a session-scoped `AnchorMap` object that lives on the shell ops layer (NOT in VFS metadata); single-token anchor allocation with fallback to multi-character anchors; anchors bind `(path, version_number, line_index, line_content)`; `edit()` validates anchors (version check then line content check), passes the anchor-validated version as `expected_version` to `session.write`, reconciles unchanged lines via Myers diff, and returns updated anchors.
  Fail-closed on stale anchors: raises `AnchorConflictError` and asks the agent to re-read.
  A raw `write()` or `delete()` through `FsOperations` invalidates all anchors for that path; a successful `edit()` atomically replaces the path's anchor state with Myers-diff reconciliation (unchanged lines keep their tokens with updated `line_index` and the new version number; changed/inserted lines get new tokens; deleted lines are removed).
  `ConflictError`/`VersionCollisionError` from `session.write` is surfaced as `AnchorConflictError` — never retried, because a conflict means the anchors are stale.
  Anchored operations (`cat`/`head`/`tail`, `edit`) decode content as strict UTF-8; undecodable content yields a structured error and no anchors.
  Line model: split on `\n` only; `\r` remains part of line content; trailing-newline presence is preserved through `edit()`.
- **`vfs.execute(code, namespace_id, principal_id, provider, timeout, resource_limits, cwd="/")`**: the VFS-layer entrypoint; enforces the `execute` permission (default-deny, already stored on the model) against the provided `cwd` path, which must be a canonical path; constructs a `Session` bound to that cwd via `session.cd(cwd)` (which also enforces read permission on the cwd); builds `FsOperations`, dispatches to the named provider; raises for caller-side errors (bad args, unknown provider, `PermissionDeniedError`) and returns structured `ExecutionResult` failures for all errors arising during execution.
  Sandbox filesystem access is NOT confined to the execute scope — it is governed by the principal's normal read/write/delete permissions; the `execute` permission gates entry at a scope, while per-operation permissions gate every FS call inside.
- **Provider registry and `VFSConfig.execution_providers`**: URI-style registration of execution providers (already sketched in §9.1); `resolve_execution_provider(name, config)` factory following the same lazy-import + "install extra" pattern as the metadata/blob resolvers.
- **`MontyExecutionProvider`** (optional `monty` extra): wraps pydantic-monty v0.0.18's `Monty` class via `await monty.run_async(...)`, passing the async `FsOperations` callables directly as `external_functions` (the type stubs confirm coroutine-returning external functions are awaited on the host event loop); `run_async` resolves directly to the output value; constructs `ExecutionResult` from that output or from provider exceptions.
  VFS `ResourceLimits.max_memory_bytes` maps to Monty's `max_memory` field; `timeout_seconds` maps to `max_duration_secs` (field names verified at integration time).
  See Design § Monty bridging decision.

> **Testing:** `FsOperations`, shell wrappers, `AnchorMap`, and rate limiting are tested directly against a real `Session` with no execution provider. `vfs.execute` dispatch, error translation, permission gate, timeout, and Monty behavior are tested against `MontyExecutionProvider`, marked `skipif(not HAS_MONTY)`; these are normal unit tests that skip without the extra installed. Event-loop starvation is verified as an automated concurrent heartbeat test.

### Out of Scope

- Bashkit, just-bash, Eryx, PyMiniRacer, E2B (future tiers per §8.1).
- Execution-state snapshot/restore (future optional protocol methods).
- Streaming execution output.
- Cross-provider scheduling or multi-provider fan-out.
- `exec` permission enforcement on any operation other than `vfs.execute` (read/write/delete gates are unchanged).

## Approach

Build in strict dependency order — protocol, then session-bound operations, then the sandbox entry point, then the Monty adapter:

1. Define `ExecutionProvider`, `ExecutionResult`, `ExecutionCapabilities`, `ResourceLimits`, and the new `OperationBudgetExceededError` error.
2. Implement the `FsOperations` dataclass and `fs_operations_for(session, resource_limits)` factory, including the counting wrapper that raises `OperationBudgetExceededError` at `max_operations`.
   Verify all shell wrappers call into the session correctly.
3. Implement the Anchored Editing subsystem: `AnchorMap`, anchor allocation (single-token pool first, fallback to multi-character), `edit()` validation and Myers-diff reconciliation, and `AnchorConflictError`.
4. Implement `vfs.execute(...)` with `execute` permission check, Session + FsOperations construction, provider dispatch, and error translation.
5. Test host-side surfaces (`FsOperations` wrappers, `AnchorMap`, rate limiting) directly against a real `Session` with no execution provider.
   Provider-path tests (`vfs.execute` dispatch, permission gate, error translation, timeout) use `MontyExecutionProvider` with `skipif(not HAS_MONTY)`.
6. Implement `MontyExecutionProvider` behind the `monty` extra; wire `resolve_execution_provider`; add packaging entry.

## Open Questions

None blocking.
Pydantic-monty's async bridging is resolved by the published type stubs: `run_async` is coroutine-returning and awaits coroutine-returning `external_functions` on the host event loop (see `design.md` Decision (e)).
Two narrow integration-time verifications remain: (a) Monty `ResourceLimits` field names (`max_memory` and `max_duration_secs`) — confirmed at integration time against the installed stub; (b) whether CPU-bound interpretation between external calls yields to the event loop — verified by an automated concurrent heartbeat test, with the documented `start()`/`resume()` fallback if it blocks.
