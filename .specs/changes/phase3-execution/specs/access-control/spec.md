# Access Control — Delta Spec

> Change: `phase3-execution`
> Date: 2026-06-09

## MODIFIED Requirements

### Requirement: OperationGranularity

> Previously: the `execute` operation was defined in the permission model but explicitly not
> enforced — "reserved for Phase 3 execution providers."

The system SHALL enforce the `execute` permission at the `vfs.execute` entry point.
A principal that does not have `execute` permission on the provided `cwd` SHALL cause `vfs.execute` to raise `PermissionDeniedError` before any session, `FsOperations`, or provider is constructed — consistent with every other VFS operation.
All other operation types (`read`, `write`, `delete`, `admin`) are unchanged.

#### Scenario: ExecutePermissionEnforced

- **GIVEN** a principal with `{read, write}` but not `{execute}` on `/workspace/`
- **WHEN** `vfs.execute(code, namespace_id, principal_id, ..., cwd="/workspace/")` is called
- **THEN** `PermissionDeniedError` is raised immediately — no session is created, no FsOperations
  is constructed, and no provider dispatch occurs

#### Scenario: ExecutePermissionStorable

> Re-stated from Phase 1 for completeness; behavior is unchanged, only enforcement is new.

- **GIVEN** an admin grants `{execute}` on `/workspace/` to a principal
- **WHEN** the permission is persisted and queried
- **THEN** the `{execute}` operation is present in the stored operations set
