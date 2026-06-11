# Session — Delta Spec

> Change: `phase3-execution`
> Date: 2026-06-09

## MODIFIED Requirements

### Requirement: SessionProxiesVFS

> Previously: `Session` proxied `read`, `write`, `delete`, `stat`, `list`, `search`,
> `versions`, `rollback`, `copy`, `move` — all path arguments resolved through `cwd`.

The `Session` SHALL additionally proxy `execute` so that `session.execute(code, ...)` resolves the caller's namespace and principal from the session context and delegates to `vfs.execute`.
All existing path-resolving proxy operations are unchanged.

#### Scenario: SessionExecuteProxiesToVfs

- **GIVEN** a `Session` constructed with `namespace_id` and `principal_id`
- **WHEN** `session.execute(code, provider_name="monty", ...)` is called
- **THEN** `vfs.execute(code, namespace_id=session.namespace_id, principal_id=session.principal_id, ...)` is invoked

## Technical Notes

- `session.execute` does not perform its own permission check — it delegates to `vfs.execute`, which enforces the `execute` permission gate.
- The `AnchorMap` and `FsOperations` instances are constructed inside `vfs.execute`, not on the `Session`; the `Session` does not hold anchors or shell-op state.
- `session.execute` passes the session's CURRENT `cwd` as the `cwd` argument to `vfs.execute`.
  Shell ops within the sandbox therefore start from the same working directory the session was in when `execute` was called.
  The `execute` permission is checked on that `cwd`.

### Requirement: SessionSearch (MODIFIED)

> Previously: `Session.search(query, scope, search_type)`.

The `Session.search` method gains a `find_predicates` passthrough parameter: `session.search(query, scope, search_type, find_predicates=None)`.
The `find_predicates` value is forwarded to the underlying `vfs.search` call unchanged; all other parameters and behavior are unchanged.

#### Scenario: FindPredicatesPassthrough

- **GIVEN** a `Session` and a `FindPredicates` value
- **WHEN** `session.search(query, scope, search_type, find_predicates=pred)` is called
- **THEN** `vfs.search` is invoked with the same `find_predicates` value forwarded unchanged
