# Proposal: Audit and Trace copy, move, and execute

## Intent

The observability contract is internally inconsistent with the product's trust bet (`NORTH-STAR.md` bet #2: every agent state-change is **attributable** via the append-only audit log).
`AuditLogStateChanges` enumerates the audited state-changing operations as write, delete, rollback, permission change, and GC run — but `copy` creates a new version and `move` creates a destination version _and_ a source tombstone, both state-changing and both unmentioned.
`OTelSpansOnAllOperations` omits `execute`, `copy`, and `move`.
Worst of all, `vfs.execute` — an agent running arbitrary code that mutates files — has no audit event and no span of its own, so the invocation ("principal X ran this code at cwd Y") is not attributable as a unit even though its inner writes are individually audited.

This change closes that gap so the audit and span contracts match what a state-changing operation actually is.
It is an additive correctness fix.

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
rollback audit already do); no change to the `execution` capability spec is needed.
`append_audit_event` is part of the `MetadataStore` protocol satisfied by every backend
family (relational + document), so the contract holds at the floor without a
backend-specific clause.
