# Design: Audit and Trace copy, move, and execute

## Context

- `NORTH-STAR.md` bet #2 makes attributability a product feature: every agent state-change
  must be reconstructable from the append-only audit log, and the job-to-be-done is
  answering "what did the agent do, and can I undo it?".
- The observability spec already owns the audit and span contracts for write, delete, and rollback — capabilities like versioning and file-operations do not restate them.
  This change keeps that single source of truth: all deltas land in `observability`, none in `execution`.
- Implementation reality (verified in `src/vfs/vfs.py`, `src/vfs/observability/audit.py`,
  `src/vfs/observability/tracing.py`):
  - `copy` already emits `audit_copy` (operation="copy", path=dst, version_id=new dst
    version, detail={src_path}) and a `vfs.copy` span.
  - `move` already emits `audit_move` (operation="move", path=dst, version_id=new dst
    version, detail={src_path}) and a `vfs.move` span — a single event today.
  - `execute` emits **nothing**: no span, no metrics, no audit event.
  - `test_all_vfs_operations_carry_principal_id_on_spans` already exercises `vfs.copy`
    and `vfs.move` spans; `execute` is absent from it.
- `AuditEvent` (`src/vfs/models.py`) already carries every field needed: `operation`, `path`, `version_id`, `detail` (free dict), and `trace_id`.
  No model change is required.
- `append_audit_event` is part of the `MetadataStore` protocol implemented by SQLite,
  PostgreSQL, and MongoDB adapters, so the contract holds at the floor (relational +
  document families) with no backend-specific clause.

## Decisions

### Decision: ObservabilityOwnsTheAuditAndSpanContract

**Chosen:** Express all copy/move/execute audit and span requirements in the
`observability` delta; leave the `execution` capability spec untouched.

**Rationale:** The existing structure already specifies write/delete/rollback audit and spans in `observability`, not in the capabilities that own those operations.
Splitting the execute audit/span requirement into `execution` would fork the contract and break the single source of truth.

**Alternatives considered:**

- Add an audit/span clause to `VfsExecutePermission` in the `execution` spec: rejected —
  duplicates the observability contract and invites drift.

### Decision: MoveEmitsExactlyOneAuditEvent

**Chosen:** A move produces one audit event (`operation="move"`, `path=dst`, `version_id`=the new destination version, `detail.src_path`=source), matching the existing `audit_move` implementation.
The source tombstone does **not** get its own event.

**Rationale:** A move is one agent intent.
One event preserves attributability as a single causal unit and lets a reader reconstruct both endpoints: the destination version is named directly, and the source state change (the tombstone) is recoverable from the source path's version history, correlated by the shared `trace_id` and timestamp.
Two events would fragment one intent into a phantom "create" and a phantom "delete" with no first-class link between them, and a reader scanning for deletions could mistake the tombstone for an independent `delete`.
The "can I undo it?"
question is answerable from the one event plus version history.

**Alternatives considered:**

- Two events (one for the destination version, one for the source tombstone): rejected —
  fragments a single intent and risks misattribution, as above.
- One event enriched with the source tombstone `version_id` in `detail`: deferred — a reasonable future enrichment, but it requires threading the tombstone id into `audit_move` and is not needed to reconstruct or reverse the move (the source version history already exposes it).
  Kept out of scope to stay surgical.

### Decision: ExecuteEmitsOneEnvelopeEventDistinctFromInnerEvents

**Chosen:** `execute` emits one invocation-level "envelope" audit event (`operation="execute"`, `path=cwd`, `detail={provider, success[, error_type]}`).
The file operations the executed code performs continue to emit their own per-operation events (write/delete/copy/move/rollback) independently.
The envelope neither replaces nor duplicates them.

**Rationale:** The inner per-write events answer "which files changed"; the envelope answers "who ran what code, where, and how did it end" — the unit that requirement #8 identified as missing.
They are correlated by the shared `trace_id` (the `audit()` helper stamps the active trace onto every event) and by the `vfs.execute` span parenting the inner operation spans.
Keeping the envelope free of a per-operation list avoids duplicating data already captured by the inner events and by the trace tree.

**Alternatives considered:**

- Make the envelope carry the list/count of inner operations: rejected for the mandatory contract — the `OperationCounter` lives inside `FsOperations` and is not currently surfaced to `vfs.execute`; plumbing it out is extra scope for marginal value over the trace tree.
  Deferred.
- Suppress the inner per-operation events during execute and keep only the envelope:
  rejected — would lose file-level attributability, directly weakening bet #2.

### Decision: ExecuteEventCoversSuccessAndStructuredFailure; Tier-1FailuresNotAudited

**Chosen:** Emit the execute envelope event once after provider dispatch resolves, for both the success outcome and the Tier-2 structured-failure outcome (timeout, permission, conflict, internal error, …).
Tier-1 failures that raise before dispatch (non-canonical cwd, unknown provider, denied `execute` permission) emit **no** event.

**Rationale:** A run that mutates some files and then fails mid-way is exactly where attributability matters most, so failure outcomes must be recorded.
Tier-1 failures occur before any code runs — no state can change — so not auditing them is consistent with the existing contract, where denied or malformed writes/deletes are likewise not audited.
The single emission point (after dispatch, before returning) covers both Tier-2 branches without risking a double event.

## Architecture

```text
vfs.execute(code, ns, principal, provider, cwd)
  ├─ Tier-1 validation + execute-permission check   (raises → no span, no audit)
  └─ with vfs_span("execute", {ns, path=cwd, principal})        ← parent span
       ├─ provider.execute(code, fs_ops, limits)
       │     └─ fs_ops.write / read / edit ...
       │           └─ session.write → vfs.write
       │                 ├─ vfs_span("write", ...)               ← CHILD of vfs.execute
       │                 └─ audit_write(...)  → AuditEvent(op="write", trace_id=T)
       ├─ audit_execute(...) → AuditEvent(op="execute", path=cwd, trace_id=T,
       │                                   detail={provider, success[, error_type]})
       └─ record_op("execute", duration, {ns})                  ← metrics parity

Correlation key: every event above shares trace_id = T (active trace),
and every inner span is a descendant of vfs.execute.
```

## Risks

- **Execute envelope emitted on the failure path could be missed if placed only on the success branch.**
  Mitigation: emit from a single point reached by both the success and the structured-failure outcomes; cover both with `ExecuteAudited` and `ExecuteFailureAudited` test tasks.
- **Span parenting depends on the inner FS operations running inside the `vfs.execute` span's active context.**
  Mitigation: wrap the provider dispatch (not just the pre-dispatch setup) in the span context manager; assert descendant relationship in `ExecuteSpanParentsInnerOperations`.
- **Contract-floor regression on a document backend.**
  Mitigation: the contract only uses `append_audit_event` and free-form `detail`, already implemented by every adapter; the envelope adds no new `AuditEvent` field, so no backend migration is needed.
