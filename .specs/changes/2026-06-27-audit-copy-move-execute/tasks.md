# Tasks: Audit and Trace copy, move, and execute

## Execute instrumentation — span + metrics (foundation)

- [ ] Wrap the post-validation body of `vfs.execute` (provider resolution, session/FsOperations construction, and provider dispatch) in a `vfs_span("execute", {"vfs.namespace": namespace_id, "vfs.path": cwd, "vfs.principal_id": principal_id}, otel_enabled=...)` so the inner FS-operation spans become descendants; start the wall-clock timer (`t0`) before the span as the other operations do.
- [ ] Add a `record_op("execute", duration_ms, {"vfs.namespace": namespace_id}, otel_enabled=...)` call after dispatch resolves, for metric parity with other operations.
- [ ] Test (`OTelSpansOnAllOperations` / `ExecuteSpan`): assert a `vfs.execute` span is created with `vfs.namespace`, `vfs.path`, and `vfs.principal_id` attributes when otel is enabled.
  Extend `test_all_vfs_operations_carry_principal_id_on_spans` to also drive `vfs.execute` and include `"vfs.execute"` in `expected_ops`.
- [ ] Test (`OTelSpansOnAllOperations` / `ExecuteSpanParentsInnerOperations`): using the in-memory span exporter, run code that performs a write through `FsOperations` and assert the captured `vfs.write` span is a descendant of the `vfs.execute` span (same `trace_id`, parent chain).
  Guard with `skipif(not HAS_MONTY, ...)` if it needs the monty provider.
- [ ] Test (`OTelSpansOnAllOperations` / `NoOpWhenDisabled` regression): assert `vfs.execute` creates no span and raises no error when `otel_enabled=False`.

## Execute audit event (depends on the execute span for trace_id correlation)

- [ ] Add `audit_execute(meta_store, *, namespace_id, principal_id, cwd, provider_name, success, error_type=None, audit_log_enabled)` to `src/vfs/observability/audit.py` constructing `AuditEvent(operation="execute", path=cwd, detail={"provider": provider_name, "success": success, ...("error_type": error_type if not success)})` and persisting via the shared `audit()` helper (which stamps `trace_id`).
- [ ] Call `audit_execute` once in `vfs.execute`, inside the `vfs.execute` span and after provider dispatch resolves, for both the success outcome and the Tier-2 structured-failure outcome (read `success`/`error_type` from the resolved `ExecutionResult`).
  Do not emit on Tier-1 raises (non-canonical cwd, unknown provider, denied execute permission).
- [ ] Test (`AuditLogStateChanges` / `ExecuteAudited`): with `audit_log_enabled=True` and execute permission, run code that succeeds and assert an `AuditEvent` with `operation="execute"`, `path=cwd`, and `detail` containing the provider name and a success outcome is persisted.
- [ ] Test (`AuditLogStateChanges` / `ExecuteFailureAudited`): drive a Tier-2 structured failure (e.g. operation-budget exceeded or timeout) and assert an `AuditEvent` with `operation="execute"`, `path=cwd`, and `detail` recording the failure outcome and `error_type` is persisted.
- [ ] Test (`AuditLogStateChanges` / `ExecuteInnerWritesIndependentlyAudited`): run code that performs a write and assert both an `operation="write"` event and an `operation="execute"` event are persisted as separate events sharing the same `trace_id`.
- [ ] Test (`AuditLogStateChanges` regression): assert no execute event is persisted on a Tier-1 denial (principal lacks execute permission on cwd) — `PermissionDeniedError` raises and nothing is audited.
- [ ] Test (`AuditLogStateChanges` regression): assert `audit_execute` persists nothing when `audit_log_enabled=False`.

## Copy / move contract regression (independent; pins already-implemented behavior)

- [ ] Test (`AuditLogStateChanges` / `CopyAudited`): perform a copy and assert an `AuditEvent` with `operation="copy"`, `path=dst`, the new destination `version_id`, and `detail.src_path` is persisted.
- [ ] Test (`AuditLogStateChanges` / `MoveAudited`): perform a move and assert **exactly one** `AuditEvent` is persisted with `operation="move"`, `path=dst`, the new destination `version_id`, and `detail.src_path` (assert the count is one, guarding the single-event decision).
- [ ] Test (`OTelSpansOnAllOperations` / `CopySpan` and `MoveSpan`): assert `vfs.copy` and `vfs.move` spans exist with `vfs.namespace`, `vfs.path`, and `vfs.principal_id` attributes (extend or rely on `test_all_vfs_operations_carry_principal_id_on_spans`, which already drives copy and move).

## Verification

- [ ] Run `uv run pytest tests/unit/test_observability.py tests/unit/test_execute.py` (and the monty-gated execute tests with `uv sync --extra monty`) and confirm green.
