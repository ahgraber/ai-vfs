# Proposal: Audit/trace copy, move, execute — and harden the execution & search contracts

## Intent

The observability contract is internally inconsistent with the product's trust bet (`NORTH-STAR.md` bet #2: every agent state-change is **attributable** via the append-only audit log).
`AuditLogStateChanges` enumerates the audited state-changing operations as write, delete, rollback, permission change, and GC run — but `copy` creates a new version and `move` creates a destination version _and_ a source tombstone, both state-changing and both unmentioned.
`OTelSpansOnAllOperations` omits `execute`, `copy`, and `move`.
Worst of all, `vfs.execute` — an agent running arbitrary code that mutates files — has no audit event and no span of its own, so the invocation ("principal X ran this code at cwd Y") is not attributable as a unit even though its inner writes are individually audited.

The phase-3 pre-merge review surfaced adjacent gaps in the same trust bet (#2: contained + attributable): the sandbox's native filesystem mount (`open`/`pathlib`) bypassed the `ResourceLimits` operation budget and read/write size caps (a host-OOM / budget-bypass vector); agent-supplied `grep` patterns ran on a backtracking regex engine that an adversarial pattern could hang the host with; and the just-bash provider reported every run as a success, hiding failures.
These are contract-level hardening of the same execution surface this change already touches, so they ride along as `execution` and `search` delta specs and sync together with the observability delta.

This change closes those gaps so the audit, span, resource-limit, and regex contracts match what a safe, attributable, contained state-changing operation actually is.
It is an additive correctness/security fix.

## User Stories

### Story: attributable-copy-move

As an operator embedding agents into a product, I want every file `copy` and `move` an
agent performs recorded in the append-only audit log and traced, so that I can reconstruct
and reverse any change an agent made to my files after the fact.

### Story: attributable-execute

As an operator embedding agents into a product, I want each `vfs.execute` invocation
recorded as a single attributable audit event and a single trace that parents the code's
inner file operations, so that I can answer "which principal ran what code, where, and did
it succeed" as one unit — even when the code mutates many files.

### Story: bounded-sandbox-resources

As an operator, I want a sandboxed agent's file operations to be bounded by `ResourceLimits`
(operation count, read size, write size) even when the agent uses native file I/O (`open`,
`pathlib`) rather than the injected shell verbs, so that a runaway or adversarial agent cannot
exhaust host memory or the operation budget through the native mount.

### Story: dos-resistant-search

As an operator, I want agent-supplied regex search patterns to be evaluated in bounded
(linear) time, so that an adversarial pattern cannot hang the workspace's host process and deny
service to every other in-flight operation.

### Story: honest-execution-outcomes

As an operator, I want a sandboxed command that fails (non-zero exit) reported as a failure with
its diagnostics, so that I can trust the recorded outcome of code an agent ran instead of seeing
a false success.

## Scope

**In scope:**

- `OTelSpansOnAllOperations`: extend the enumerated operations to include `copy`, `move`,
  and `execute`, with scenarios for each.
- `AuditLogStateChanges`: extend the enumerated state-changing operations to include
  `copy`, `move`, and `execute`, with scenarios for each.
- Add an `execute`-level OTel span (`vfs.execute`) that parents the inner file-operation
  spans, plus operation-count/duration metrics, in `vfs.execute`.
- Add an `execute`-level audit event recording principal, cwd, provider, and outcome
  (success / structured-failure), distinct from the inner per-operation audit events.
- Regression tests that lock in the already-implemented `copy`/`move` audit events and
  spans against the newly-explicit contract.
- `execution` delta: the FS-port native mount enforces `ResourceLimits` (shared operation
  budget + `max_read_bytes`/`max_write_bytes`), `ResourceLimits` gains `max_write_bytes`,
  `ExecutionCapabilities` gains `enforces_memory_limit`, the `execute` protocol signature gains
  `fs_port`, the error table gains `ResourceLimitExceededError`, and the just-bash provider
  reports non-zero exits as failures (`error_type="nonzero_exit"`).
- `search` delta: regex content search uses a linear-time (RE2) engine — no catastrophic
  backtracking, unsupported features (backreferences/lookaround) yield no matches, and REGEX
  results are identical across backends.

**Out of scope:**

- Enriching the `move` audit event with the source tombstone's `version_id` (the source
  state change is discoverable via the source path's version history, correlated by
  `trace_id`); a single event is sufficient — see `design.md`.
- Recording an operation count on the `execute` event (would require plumbing the
  `OperationCounter` out of `FsOperations`); deferred.
- Auditing Tier-1 `execute` failures (non-canonical cwd, unknown provider, denied
  `execute` permission) — no code runs and no state can change, consistent with how
  denied writes/deletes are not audited today.
- Audit-table archival or rotation (already deferred in `AuditLogAppendOnly`).
- Any redesign of the audit or tracing infrastructure; no new `AuditEvent` fields.

## Approach

`copy` and `move` already emit `audit_copy` / `audit_move` events and `vfs.copy` /
`vfs.move` spans in `src/vfs/vfs.py`; the only deltas for them are the spec text and
regression tests that pin the existing behavior to the now-explicit contract.

`execute` is the real implementation work.
Wrap the post-validation body of `vfs.execute` in a `vfs_span("execute", ...)` so the inner FS-operation spans (driven through `FsOperations`) become children of it — that span parenting is what makes the invocation attributable as a unit, and it lets the `execute` audit event inherit the active `trace_id` through the existing `audit()` helper.
Add an `audit_execute` helper that records `operation="execute"`, `path=cwd`, and `detail` carrying `provider` and the outcome (`success`, plus `error_type` on structured failure).
Emit it once after provider dispatch resolves — covering both the success and the Tier-2 structured-failure outcomes — so a partially-mutating run that then errors is still attributable.
Add a `record_op("execute", ...)` call for metric parity with the other operations.

The audit and span contracts live in the `observability` capability (as write/delete/
rollback audit already do).
`append_audit_event` is part of the `MetadataStore` protocol satisfied by every backend
family (relational + document), so the contract holds at the floor without a
backend-specific clause.

The hardening deltas live in the `execution` and `search` capabilities.
The FS-port resource enforcement is applied at the single `SessionFsPort` boundary that both sandbox providers (Monty mount, just-bash `IFileSystem`) already route through, so one change governs both — the operation budget becomes a property of the session boundary (a shared `OperationCounter` passed to both the injected verbs and the mount) rather than of the injected verbs alone.
The regex hardening swaps the in-process verification engine to RE2 uniformly across every backend's `search_text`/brute-force path; PostgreSQL additionally drops its anchor-sensitive whole-document `~` prune (which could differ from per-line matching) in favour of per-line RE2 verification, keeping REGEX results identical across backends at the contract floor.
