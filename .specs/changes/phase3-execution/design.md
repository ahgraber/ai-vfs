# Design: Phase 3 (Execution) — Sandboxed Execution Providers

## Context

Phases 1 and 2 established: `MetadataStore`/`BlobStore` ports and adapters, the `Session` CWD wrapper, permission enforcement, search dispatch, and native FTS.
The `execute` operation has been in the permission model since Phase 1 but never enforced.
The design doc (§3.4–3.6, §8) fully specifies the execution layer; this change implements it.

Four new objects compose to form the execution layer:

1. **`ExecutionProvider` protocol** — the port; one concrete implementation per sandbox tier.
2. **`FsOperations` dataclass + `fs_operations_for` factory** — the bridge between sandbox and VFS, bound to a `Session`.
3. **`AnchorMap`** (shell ops layer, Session-scoped) — session-lived anchor state for token-efficient editing.
4. **`vfs.execute`** — the VFS-layer entry point; permission gate, session/FsOps construction, provider dispatch, error translation.

Decisions are ordered by build dependency, matching `proposal.md` Scope and `tasks.md`.

## Decisions

### Decision (a): Anchor map lives on the shell ops layer, bound to the Session; invalidated per-path on write

**Chosen:** `AnchorMap` is a plain Python object constructed inside `fs_operations_for` and stored as a closed-over state object alongside the shell wrappers.
It is NOT stored in VFS metadata, NOT on the `Session`, and NOT on the `VFS` instance.
Its lifetime matches the `FsOperations` object: it exists for one `execute` call (one invocation of `fs_operations_for`).

Anchors are allocated from a fixed single-token pool on first use; when the pool is exhausted for the current session the allocator falls back to short (2–4 character) random strings that do not collide with pool entries.
Each anchor entry binds `(path, version_number, line_index, line_content)`.

Validation: resolve the anchor, check that the file's current version number equals the recorded `version_number`, then check that the line at `line_index` in the current content equals `line_content`.

A raw `write()` or `delete()` through `FsOperations` invalidates all anchors for that path — after which existing anchor tokens raise `AnchorConflictError`.
A successful `edit()` does NOT invalidate-then-reconcile; it atomically REPLACES the path's anchor state with the Myers-diff reconciliation result: unchanged lines keep their tokens with updated `line_index` and the new `version_number`; changed/inserted lines get new tokens; deleted lines are removed.
The agent must re-read after a raw `write()` on a path; after `edit()`, the returned anchor set is already reconciled and valid.

**Rationale:** Version metadata is VFS-owned and immutable; anchor tokens are a model-interface convenience with no value outside a single execution turn.
Anchors stored in metadata would require a schema migration, GC rules, and multi-session invalidation logic — none of which is needed here.
Anchors bound to `(path, version_number, line_index, line_content)` already carry enough information to detect staleness without VFS-side state.

**Alternatives considered:**

- Anchor map on the `Session` object: Session is designed as a long-lived CWD wrapper reusable across many operations; anchors are execution-turn scoped.
  Mixed lifetimes create confusing invalidation semantics.
- Anchor map in VFS metadata (per-version field): adds a schema field with no query value,
  requires GC, and couples a presentation concern to storage.

### Decision (b): `edit()` anchor validation; interplay with `expected_version` and `VersionCollisionError`

**Chosen:** `edit(path, start_anchor, end_anchor, replacement, expected_version=None)` applies
in two sequential stages:

1. **Anchor validation (pre-write):** Resolve start/end anchors from `AnchorMap`.
   Each anchor entry carries `version_number`; if the file's current version (via `session.stat`) differs from the anchor's recorded version, raise `AnchorConflictError` immediately.
   Then verify line content: read the file and check that the line at the stored `line_index` still equals the recorded `line_content`.
   If not, raise `AnchorConflictError`.
   If the caller supplies `expected_version` and it differs from the anchor's recorded `version_number`, raise `AnchorConflictError` immediately, before any write.
2. **Write:** Construct the replacement content; `edit()` SHALL always pass the anchor-validated
   `version_number` as `expected_version` to `session.write` (providing CAS protection regardless
   of whether the caller supplied `expected_version`).

If `session.write` raises `ConflictError` (CAS mismatch) or `VersionCollisionError` (concurrent
write collision), surface it as `AnchorConflictError` — never retried, because a conflict means
the current anchors are stale and the agent must re-read.

After a successful write, the path's anchor state is atomically REPLACED with the Myers-diff reconciliation result: unchanged lines keep their tokens with updated `line_index` and the new `version_number`; changed/inserted lines get new tokens; deleted lines are removed.
There is no separate `invalidate` call before `reconcile` — the replacement is atomic.

**Rationale:** Separating validation from write keeps the conflict surface narrow.
The anchor's `version_number` is a cheap pre-read staleness check (one `stat` call) before the more expensive content read; this avoids reading blob content for clearly stale anchors.
Always passing the anchor-validated version as `expected_version` ensures the write is protected by CAS even when the caller did not supply it, closing the race between validation and write.
Surfacing all write-side conflicts as `AnchorConflictError` keeps the agent's recovery path uniform: re-read, then re-edit.

**Alternatives considered:**

- Relying solely on `expected_version` for conflict detection: callers can omit it; we still
  need the anchor content check to catch line-shifted edits where the version is the same.
- Retrying on `VersionCollisionError`: a conflict means the anchors are stale regardless of
  retry outcome — the agent must re-read to get fresh anchors anyway.

### Decision (c): `max_operations` enforced by a counting wrapper in `fs_operations_for`; composes with search read budget independently

**Chosen:** `fs_operations_for(session, resource_limits)` wraps every async callable in `FsOperations` with a shared `OperationCounter` that increments on each call.
When the counter reaches `resource_limits.max_operations`, the next call raises `OperationBudgetExceededError` immediately without invoking the underlying VFS operation.

The `max_operations` counter counts VFS callback invocations (one call to `cat`, `grep`,
`write`, etc. = one operation), not bytes, lines, or search results.

The search read budget (`SearchLimits.max_content_reads`) is a separate counter managed entirely inside `vfs.search` / `NativeTextSearch`; it is NOT shared with or folded into `max_operations`.
The two budgets compose independently: a single `grep` call costs one operation against `max_operations` AND may internally exhaust `max_content_reads` on the search path.

When `grep` fires `ReadBudgetExceededError` (straggler budget exhausted) or `ReindexRequiredError` / `IndexUnavailableError` (cold index), those exceptions propagate through `FsOperations` to the sandbox.
The `MontyExecutionProvider` (and `vfs.execute` error translation) catches these and maps them to structured `ExecutionResult` failure values — no raw traceback, no host path leak.

**Rationale:** A single unified counter would conflate "number of index lookups" with "number of agent-visible VFS calls," making the limit hard to reason about.
Keeping the two budgets separate preserves the existing `SearchLimits` contract from `phase2-search` and gives operators independent knobs.

**Alternatives considered:**

- Count search straggler reads toward `max_operations`: couples search internals to the
  execution surface; breaks existing `SearchLimits` contract.
- No `max_operations` wrapper; rely solely on provider-level limits: does not defend against
  unbounded loops that each make cheap VFS calls (e.g., reading 10k tiny files).

### Decision (d): `grep` maps to `vfs.search(type=REGEX)` with `SearchLimits` propagated; sandbox sees `ReadBudgetExceededError`/`ReindexRequiredError` as structured errors

**Chosen:** The `grep(pattern, path, recursive=True)` shell wrapper calls `session.search(query=pattern, scope=resolved_path, search_type=SearchType.REGEX)`.
The `SearchLimits` used inside that search are the VFS default (from `VFSConfig`); the shell op does not override them.
`grep` does NOT accept a `max_content_reads` override parameter — that is a VFS/config concern, not a shell-command option.

When `session.search` raises `ReadBudgetExceededError`, `ReindexRequiredError`, or
`IndexUnavailableError`, the `grep` wrapper re-raises the exception unchanged.
`fs_operations_for` does NOT catch these internally; they bubble to `vfs.execute`'s error
translation layer.
`vfs.execute` maps them to `ExecutionResult(success=False, error_type="search_unavailable", error_message=<actionable text>)`, with no host path or traceback in `error_message`.

Sandbox code that calls `grep(...)` may therefore see a `RuntimeError` (or equivalent) with a message like `"Search index unavailable — call vfs.reindex() first"` injected by Monty's external-function bridge.
The design doc (§3.4) is explicit that `search_meta` and `read_content` are internal to the VFS/search boundary; Monty sees only the curated shell functions and their results.

**Rationale:** Surfacing structured errors back to sandbox code is the correct behavior — the agent can react (e.g., switch to `find` + `cat` if grep is unavailable) rather than crash.
Hiding them silently would produce wrong answers.

**Alternatives considered:**

- `grep` swallows `ReindexRequiredError` and returns an empty list: produces false-empty results
  on a cold index — a silent incorrect answer.
- Expose `max_content_reads` as a `grep` parameter: leaks internal search plumbing to the
  agent's shell interface.

### Decision (e): Monty async bridging via native `run_async`; async VFS callables passed directly as `external_functions`

**Chosen:** `MontyExecutionProvider.execute` is an `async def` method that calls `await monty.run_async(inputs=..., limits=..., external_functions=...)` directly.

The pydantic-monty type stubs ([`_monty.pyi`](https://github.com/pydantic/monty/blob/main/crates/monty-python/python/pydantic_monty/_monty.pyi)) confirm `run_async(...) -> Coroutine[Any, Any, Any]` and document that **"external functions that return coroutines are awaited on the Python event loop."**
The async `FsOperations` callables (e.g., `session.read`) are therefore passed as-is in the `external_functions` dict — no thread pool, no `run_coroutine_threadsafe`, no captured-loop bookkeeping.
Everything runs on the host event loop.

Result handling: `run_async` resolves to the execution output; external-function exceptions propagate through Monty's `ExternalResult` protocol (`return_value` | `exception`), so VFS errors raised inside a callable surface in the sandbox as exceptions and, uncaught, fail the execution — which the adapter translates per Decision (f).
The current binding is against **pydantic-monty v0.0.18** (alpha, Development Status 3).

**Risks flagged:**

- **Event-loop blocking between external calls:** `run_async` awaits external functions cooperatively, but it is unverified whether CPU-bound sandbox interpretation _between_ external calls yields to the event loop or blocks it.
  An automated concurrent heartbeat test verifies this: a background `asyncio.Task` must keep ticking while a compute-heavy sandbox script runs; if the heartbeat stalls, the adapter switches to Monty's `start()`/`FunctionSnapshot.resume()` stepping protocol with `asyncio.to_thread` around the step calls, keeping callables on the loop.
- **Alpha API stability:** pydantic-monty 0.0.18 is alpha; method signatures, `ResourceLimits` fields, and return types may change in any release.
  The adapter should pin the minor version in the `monty` extra and expose a version-check guard.
- **`ResourceLimits` field mapping and name collision:** pydantic-monty exports its own `ResourceLimits` type; the adapter imports it aliased (e.g., `MontyResourceLimits`) and maps: VFS `timeout_seconds` → Monty `max_duration_secs`; VFS `max_memory_bytes` → Monty `max_memory`.
  Both field names are verified against the installed stub at integration time; unmapped fields are documented as unenforced at the provider level.

**Alternatives considered:**

- `asyncio.to_thread(monty.run, ...)` with `run_coroutine_threadsafe` bridging for the callables: the pre-stub design; now strictly worse — adds a thread hop and loop-capture fragility that the confirmed `run_async` contract makes unnecessary.
  Retained only as the fallback shape if the event-loop-blocking risk above materializes (and then via `start()`/`resume()`, not callable bridging).
- Pure `start()`/`resume()` stepping loop as the primary integration: maximum host control (per-call inspection of `FunctionSnapshot`), but reimplements scheduling the `run_async` contract already provides; revisit if per-call host-side policy (e.g., audit per sandbox call) is ever required.

### Decision (f): Two-tier error contract at the `vfs.execute` boundary; no host path or traceback in `ExecutionResult.error_message`

**Chosen:** `vfs.execute` uses a two-tier error contract:

**Tier 1 — raises for caller-side errors (before dispatch):**
`vfs.execute` raises for errors that indicate a bad call, consistent with every other VFS operation:

- `ValueError` — malformed arguments (e.g. non-canonical `cwd`), unknown provider name.
- The missing-extra error from `resolve_execution_provider` (raised as a descriptive `ImportError`-derived type).
- `PermissionDeniedError` — principal lacks `execute` permission on `cwd` (raised, not returned as `ExecutionResult`).

**Tier 2 — returns `ExecutionResult(success=False, ...)` for errors arising during execution:**
`vfs.execute` wraps the provider dispatch in a `try/except` block that catches all exceptions arising after dispatch begins and translates them to structured `ExecutionResult` failures with no raw traceback, host path, or adapter-internal detail in `error_message`.

Errors arising during execution and their mappings:

| Exception                      | `error_type`           | `error_message`                             |
| ------------------------------ | ---------------------- | ------------------------------------------- |
| `PermissionDeniedError`        | `"permission_denied"`  | `"Access denied to path"` (no path exposed) |
| `NotFoundError`                | `"not_found"`          | `"File not found"`                          |
| `ConflictError`                | `"conflict"`           | `"Version conflict; re-read and retry"`     |
| `VersionCollisionError`        | `"conflict"`           | `"Concurrent write; retry"`                 |
| `OperationBudgetExceededError` | `"budget_exceeded"`    | `"Operation limit reached"`                 |
| `AnchorConflictError`          | `"anchor_conflict"`    | `"Anchors stale; re-read file"`             |
| `ReadBudgetExceededError`      | `"search_unavailable"` | `"Search read budget exhausted; reindex"`   |
| `ReindexRequiredError`         | `"search_unavailable"` | `"Index cold; run vfs.reindex()"`           |
| `IndexUnavailableError`        | `"search_unavailable"` | `"Search index unavailable"`                |
| Unexpected `Exception`         | `"internal_error"`     | `"Execution error"` (no details)            |

Inside the sandbox, FS-callable errors surface as catchable exceptions via Monty's `ExternalResult` exception path; only uncaught errors terminate execution and flow to the translation table above.
The provider sandbox (Monty) may also raise its own exceptions (timeout, memory limit, syntax error in the code string); these propagate from `MontyExecutionProvider.execute` as structured `ExecutionResult` failures.
`vfs.execute` also wraps provider dispatch in `asyncio.wait_for(..., timeout)` (the outer end-to-end timeout); on expiry the provider task is cancelled and `ExecutionResult(success=False, error_type="timeout")` is returned.
Monty's own `max_duration_secs` is a second, inner layer.

**Rationale:** Raising for bad-call errors (unknown provider, missing permission) is consistent with every other VFS operation and gives callers a clear distinction between "you called this wrong" and "execution ran but failed."
Returning `ExecutionResult` for execution-time errors gives the agent a structured, predictable response; raw tracebacks would leak host directory paths, adapter internals, and SQL/Mongo query text into the agent's context.

**Alternatives considered:**

- Always return `ExecutionResult` even for bad-call errors: inconsistent with the rest of the VFS API; makes it impossible to distinguish a badly-formed call from a legitimate execution failure.
- Let providers be responsible for their own error translation: the provider would need to know the full VFS exception hierarchy; boundary duplication.
- Re-raise VFS errors verbatim: leaks host internals into agent context.

## Architecture

```text
vfs.execute(code, namespace_id, principal_id, provider_name, timeout, resource_limits, cwd="/")
    │
    ├── [raises ValueError]     bad args (non-canonical cwd, unknown provider name)
    ├── [raises PermissionDeniedError]  check_permission(principal_id, namespace_id, cwd, "execute")
    │
    ├── session = Session(vfs, namespace_id, principal_id)
    ├── session.cd(cwd)         ← binds session to cwd; also enforces read permission on cwd
    ├── anchor_map = AnchorMap()           ← session-scoped, shell-ops-layer only
    ├── fs_ops = fs_operations_for(session, resource_limits, anchor_map)
    │              wraps each callable in OperationCounter(max=resource_limits.max_operations)
    │
    ├── provider = resolve_execution_provider(provider_name, config)
    │
    ├── try:
    │     result = await asyncio.wait_for(
    │                 provider.execute(code, fs_ops, resource_limits),
    │                 timeout=timeout)
    │   except asyncio.TimeoutError → ExecutionResult(success=False, error_type="timeout")
    │   except VFSError → ExecutionResult(success=False, error_type=..., error_message=...)
    │   (sandbox FS access governed by principal's normal read/write/delete permissions;
    │    execute permission gates entry only — not filesystem scope)
    │
    └── return ExecutionResult

FsOperations (shell wrappers, Session-bound):
    cd(path)         → session.cd(path)
    pwd()            → session.pwd()
    cat(path)        → session.read(path)  + anchor emit (strict UTF-8; binary → structured error)
    head(path, n)    → session.read(path)[:n_lines]  + anchor emit (sliced lines only)
    tail(path, n)    → session.read(path)[-n_lines:]  + anchor emit (sliced lines only)
    ls(path, ...)    → session.list(path)  → [{name, path, is_dir, version_number, updated_at},...]
                       ls(long=True) adds size via batched VersionMeta lookup (size lives on VersionMeta)
    grep(pat, path)  → session.search(path, pat, REGEX, find_predicates=...)
    find(path, ...)  → session.search(path, predicate=FindPredicates(...), find_predicates=...)
    glob(pattern)    → session.search(path, pattern, GLOB)
    write(path, data)→ session.write(path, data)  + AnchorMap.invalidate(path)
    edit(...)        → anchor validation → session.write(expected_version=anchor_version)
                       → AnchorMap atomic replace (Myers-diff reconciliation)

    All wrapped in OperationCounter (shared, increments on every call)
    max_read_bytes: cat/head/tail raise structured error for oversized content (no host OOM)
    max_result_items: grep/find/ls truncate with truncation flag when result count exceeded

AnchorMap:
    allocate(path, version_number, lines)  → dict[line_idx, anchor_token]
                                             entries bind (path, version_number, line_index, line_content)
    validate(anchor, path)                 → (version_number, line_content) | AnchorConflictError
    invalidate(path)                       → drops all entries for path (raw write/delete path)
    reconcile(path, old_lines, new_lines, version_number)
                                           → Myers diff → atomically replaces anchor state for path
                                             (edit() path — no prior invalidate call)

MontyExecutionProvider (optional `monty` extra):
    execute(code, fs_ops, resource_limits):
        limits = MontyResourceLimits(
            max_duration_secs=resource_limits.timeout_seconds,
            max_memory=resource_limits.max_memory_bytes,   # field names verified at integration time
        )
        output = await monty.run_async(
            inputs={}, limits=limits,
            external_functions=fs_ops_as_dict(fs_ops))    # async callables awaited on host loop
        return ExecutionResult(success=True, output=output)
```

## Risks

- **pydantic-monty alpha instability**: v0.0.18 is Development Status 3 (alpha).
  Method signatures, `ResourceLimits` fields, and thread-safety semantics may change without notice.
  _Mitigation:_ pin minor version in the `monty` extra; isolate the adapter behind a thin `MontyExecutionProvider` class so changes are contained; all non-Monty tests run without the extra.
- **CPU-bound sandbox interpretation may block the event loop**: `run_async` awaits external functions cooperatively (confirmed by the type stubs), but whether interpretation _between_ external calls yields to the loop is unverified.
  _Mitigation:_ automated concurrent heartbeat test (a background `asyncio.Task` must keep ticking while a compute-heavy sandbox script runs); if blocking is detected, fall back to the `start()`/`FunctionSnapshot.resume()` stepping protocol with `asyncio.to_thread` around step calls only (see Decision (e)).
- **`AnchorConflictError` user experience**: an agent that holds stale anchors across a multi-step edit session will get repeated `AnchorConflictError` until it re-reads.
  _Mitigation:_ the error message is actionable ("re-read the file"); `cat`/`head`/`tail` always emit fresh anchors; the scenario is explicit in the `AnchoredEditing` spec.
- **Single-token anchor pool exhaustion**: a session that reads many large files may exhaust the token pool, degrading to 2–4 character anchors.
  _Mitigation:_ the pool is a curated set of ~1–2k short identifier-safe ASCII strings chosen to be single-token across common tokenizers (best-effort; tokenizer-dependent — Dirac's approach used ~1,700 anchors curated for o200k_base).
  The multi-character fallback covers exhaustion; practical sessions will not exhaust the pool.

## Verification Notes

All SHALL requirements are covered by runnable evidence.

`FsOperations`, shell wrappers, `AnchorMap`, and rate limiting are tested directly as host-side objects against a real `Session` — no execution provider involved.

`vfs.execute` dispatch, error translation, permission gate, timeout, and Monty behavior are tested against `MontyExecutionProvider` (the real provider).
These tests are marked `pytest.mark.skipif(not HAS_MONTY, reason="pydantic-monty not installed")` and skip automatically in environments without the extra; they run normally in dev (`uv sync --extra monty`).
No "user-run" designation is needed — they are normal unit tests gated by an import check.

The event-loop starvation test is an automated `asyncio` test: a concurrent heartbeat task must keep ticking at regular intervals while a compute-heavy sandbox script executes.
If the heartbeat stalls, the `start()`/`resume()` fallback (Decision (e)) is applied.

No Verification Waivers are required.
