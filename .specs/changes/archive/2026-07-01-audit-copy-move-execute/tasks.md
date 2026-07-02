# Tasks: Audit and Trace copy, move, and execute

## Execute instrumentation — span + metrics (foundation)

- [x] Wrap the post-validation body of `vfs.execute` (provider resolution, session/FsOperations construction, and provider dispatch) in a `vfs_span("execute", {"vfs.namespace": namespace_id, "vfs.path": cwd, "vfs.principal_id": principal_id}, otel_enabled=...)` so the inner FS-operation spans become descendants; start the wall-clock timer (`t0`) before the span as the other operations do.
- [x] Add a `record_op("execute", duration_ms, {"vfs.namespace": namespace_id}, otel_enabled=...)` call after dispatch resolves, for metric parity with other operations.
- [x] Test (`OTelSpansOnAllOperations` / `ExecuteSpan`): assert a `vfs.execute` span is created with `vfs.namespace`, `vfs.path`, and `vfs.principal_id` attributes when otel is enabled.
  Extend `test_all_vfs_operations_carry_principal_id_on_spans` to also drive `vfs.execute` and include `"vfs.execute"` in `expected_ops`.
  _(done: `test_observability.py::TestSpanAttributes`)_
- [x] Test (`OTelSpansOnAllOperations` / `ExecuteSpanParentsInnerOperations`): using the in-memory span exporter, run code that performs a write through `FsOperations` and assert the captured `vfs.write` span is a descendant of the `vfs.execute` span (same `trace_id`, parent chain). _(done via a fake provider that writes; no monty extra needed: `test_observability.py::TestExecuteObservabilityContract::test_execute_span_parents_inner_write`)_
- [x] Test (`OTelSpansOnAllOperations` / `NoOpWhenDisabled` regression): assert `vfs.execute` creates no span and raises no error when `otel_enabled=False`. _(done: `...::test_execute_creates_no_span_when_otel_disabled`)_

## Execute audit event (depends on the execute span for trace_id correlation)

- [x] Add `audit_execute(meta_store, *, namespace_id, principal_id, path, provider, success, error_type=None, audit_log_enabled)` to `src/vfs/observability/audit.py` constructing `AuditEvent(operation="execute", path=path, detail={"provider": provider, "outcome": "success" | "failure", ...("error_type": error_type on failure)})` and persisting via the shared `audit()` helper (which stamps `trace_id`). _(param `path` mirrors `audit_write`/`audit_delete`; `detail.outcome` matches the delta spec's "success/failure outcome" wording.)_
- [x] Call `audit_execute` once in `vfs.execute`, inside the `vfs.execute` span and after provider dispatch resolves, for both the success outcome and the Tier-2 structured-failure outcome (read `success`/`error_type` from the resolved `ExecutionResult`).
  Do not emit on Tier-1 raises (non-canonical cwd, unknown provider, denied execute permission).
- [x] Test (`AuditLogStateChanges` / `ExecuteAudited`): with `audit_log_enabled=True` and execute permission, run code that succeeds and assert an `AuditEvent` with `operation="execute"`, `path=cwd`, and `detail` containing the provider name and a success outcome is persisted. _(done: `test_execute.py::TestExecuteAudited::test_execute_success_is_audited`; helper-level: `test_observability.py::TestAuditLog::test_audit_execute_records_success`)_
- [x] Test (`AuditLogStateChanges` / `ExecuteFailureAudited`): drive a Tier-2 structured failure and assert an `AuditEvent` with `operation="execute"`, `path=cwd`, and `detail` recording the failure outcome and `error_type` is persisted. _(done: `test_execute.py::TestExecuteAudited::test_execute_failure_is_audited_with_error_type`; helper-level: `...::test_audit_execute_records_failure_with_error_type`)_
- [x] Test (`AuditLogStateChanges` / `ExecuteInnerWritesIndependentlyAudited`): run code that performs a write and assert both an `operation="write"` event and an `operation="execute"` event are persisted as separate events sharing the same `trace_id`. _(done: `test_observability.py::TestExecuteObservabilityContract::test_execute_and_inner_write_share_trace_and_both_audited`)_
- [x] Test (`AuditLogStateChanges` regression): assert no execute event is persisted on a Tier-1 denial (principal lacks execute permission on cwd) — `PermissionDeniedError` raises and nothing is audited. _(done: `...::test_tier1_permission_denial_not_audited`)_
- [x] Test (`AuditLogStateChanges` regression): assert `audit_execute` persists nothing when `audit_log_enabled=False`. _(done: `...::test_execute_not_audited_when_disabled`)_

## Copy / move contract regression (independent; pins already-implemented behavior)

- [x] Test (`AuditLogStateChanges` / `CopyAudited`): perform a copy and assert an `AuditEvent` with `operation="copy"`, `path=dst`, the new destination `version_id`, and `detail.src_path` is persisted. _(done: `test_observability.py::TestCopyMoveAuditRegression::test_copy_audited`)_
- [x] Test (`AuditLogStateChanges` / `MoveAudited`): perform a move and assert **exactly one** `AuditEvent` is persisted with `operation="move"`, `path=dst`, the new destination `version_id`, and `detail.src_path`. _(done: `...::test_move_audited_exactly_once`)_
- [x] Test (`OTelSpansOnAllOperations` / `CopySpan` and `MoveSpan`): assert `vfs.copy` and `vfs.move` spans exist with `vfs.namespace`, `vfs.path`, and `vfs.principal_id` attributes. _(covered by `test_all_vfs_operations_carry_principal_id_on_spans`, which drives copy and move.)_

## Execution hardening — FS-port resource envelope (bounded-sandbox-resources)

- [x] Delta spec: `specs/execution/spec.md` — `ExecutionProtocol` (4-arg `execute`, `max_write_bytes`, `enforces_memory_limit`, cross-surface enforcement), `FsPortContract` (mount enforces caps + budget → `ResourceLimitExceededError`), `VfsExecuteErrorTranslation` (new error row), `JustBashProvider` (non-zero exit).
- [x] Add `ResourceLimitExceededError` (`src/vfs/errors.py`) and `max_write_bytes` / `enforces_memory_limit` fields (`src/vfs/protocols/execution.py`).
- [x] `SessionFsPort` enforces read/write size caps and charges a shared `OperationCounter`; `fs_operations_for` accepts the shared counter; `vfs.execute` constructs one counter and passes it to both, and translates `ResourceLimitExceededError → "budget_exceeded"` (`src/vfs/execution/fs_port.py`, `fs_ops.py`, `vfs.py`).
- [x] Provider `capabilities()` set `enforces_memory_limit` (Monty `True`, just-bash `False`).
- [x] Tests: `test_fs_port.py::TestFsPortResourceGovernance` (read/write cap refusal, shared budget across verbs + mount, no-limits passthrough); `test_execution_protocol.py` capability feature-detection.

## Execution hardening — just-bash honest outcomes (honest-execution-outcomes)

- [x] just-bash provider returns `success=(exit_code==0)`, surfacing stderr as `error_message` and `error_type="nonzero_exit"` (`src/vfs/execution/just_bash_provider.py`).
- [x] Test: `test_just_bash_provider.py::TestBashNonZeroExitReportsFailure`.

## Search hardening — linear-time regex (dos-resistant-search)

- [x] Delta spec: `specs/search/spec.md` — `RegexContentSearch` uses linear-time RE2, unsupported features yield no matches, results identical across backends.
- [x] Add `google-re2` dependency and `src/vfs/search/_regex.py` (`compile_line_regex`, `RegexCompileError`); swap the in-process verification in `search/default.py`, `stores/sqlite_metadata.py`, and `stores/postgres_metadata.py` to RE2; drop PostgreSQL's anchor-sensitive `~` prune (per-line verification).
- [x] Tests: `test_regex_engine.py` (linear-time on `(a+)+$`, unsupported-syntax handling); existing `test_native_text_search.py` / `test_search_*` remain green.

## Verification

- [x] Run `uv run pytest tests/unit/test_observability.py tests/unit/test_execute.py` and confirm green. _(25 + 57 pass; monty-gated execute tests green with the `monty` extra installed.)_
- [x] Full unit suite green: `uv run pytest -n auto -m "not isolate" tests/unit/` — 506 passed, lint + format clean.
- [x] Integration (podman) suite, including the PostgreSQL regex path, run outside the sandbox and confirmed green by the user.
