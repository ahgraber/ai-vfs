# Shell Context — Design

**Change:** `shell-context` **Date:** 2026-04-04

## Context

The VFS core is stateless with respect to caller context.
All existing operations accept only absolute paths and have no notion of a "current" position.
Session and CWD state is a caller-side concern — the same separation POSIX uses (the filesystem knows nothing about CWD; the process does).

The `copy` and `move` operations added in the `shell-context` change are also proxied
through Session, so both source and destination paths benefit from CWD resolution.

## Decisions

### D1: Session is a stateful wrapper, not a VFS feature

**Rationale:** VFS is designed to be stateless and independently testable.
Embedding CWD in VFS would require threading session state through every method signature or storing it in the VFS instance, coupling VFS to a usage pattern it shouldn't own.
Session is a thin object that holds `(vfs, namespace_id, principal_id, cwd)` and delegates every operation to the underlying VFS after path resolution.

**Alternative considered:** CWD as a VFS constructor argument.
Rejected — VFS instances are typically long-lived and shared across sessions.

---

### D2: Path resolution uses `posixpath`, not `pathlib.PurePosixPath`

**Rationale:** `posixpath.normpath` is a single function call with well-defined POSIX semantics,
no object allocation, and no platform ambiguity.
`PurePosixPath` would work but introduces unnecessary object creation for what is a string
transformation.

```python
import posixpath


def resolve_path(cwd: str, path: str) -> str:
    if posixpath.isabs(path):
        joined = path
    else:
        joined = posixpath.join(cwd, path)
    return posixpath.normpath(joined)
```

`normpath` handles `..`, `.`, duplicate slashes, and clamps traversal above `/`
(`normpath("/../x") == "/x"`), so no additional guard is needed.

---

### D3: CWD is ephemeral (in-memory only)

**Rationale:** Storing CWD in metadata introduces a new data category, a new metadata store method, and a schema touch.
The concrete benefit — agents recovering CWD after reconnect — is speculative at this stage.
Agents that care can re-issue `cd` on startup.

**Future path:** If persistence is needed, `cwd` could be stored as a JSON field on
the `Principal` record with minimal schema impact.

---

### D4: `cd` is permissive — no directory existence check

**Rationale:** VFS directories are implicit (they are path prefixes, not first-class objects).
Checking whether a "directory" exists requires a `list` call to see if any files exist under the prefix, which is expensive and semantically odd: `cd("/future/work/")` should be valid even if no files exist there yet, matching the bash idiom of `mkdir -p` then `cd`.
Permission check on the target prefix is the correct gate.

**Alternative considered:** Require at least one file under the prefix.
Rejected — too restrictive, inconsistent with how VFS models directories.

---

### D5: Session exposes the same method signatures as VFS

**Rationale:** Drop-in substitutability.
Code written against VFS works unchanged when handed a Session instead.
This also means Session can be used as a VFS stand-in in tests that want to exercise relative-path behavior without special casing.

## Architecture

```text
Agent / Shell layer
        │
        ▼
┌───────────────────────────────┐
│  Session                      │
│  - namespace_id: str          │
│  - principal_id: str          │
│  - cwd: str  (default "/")    │
│                               │
│  pwd() → str                  │
│  cd(path) → None              │
│  read(path, ...) → bytes      │   ← resolve_path(cwd, path) before each call
│  write(path, ...) → ...       │
│  delete(path, ...) → None     │
│  stat(path, ...) → FileMeta   │
│  list(path, ...) → ...        │
│  copy(src, dst, ...) → ...    │
│  move(src, dst, ...) → ...    │
│  search(path, ...) → ...      │
│  versions(path, ...) → ...    │
│  rollback(path, ...) → ...    │
└──────────────┬────────────────┘
               │ absolute paths only
               ▼
┌───────────────────────────────┐
│  VFS                          │
│  (stateless, unchanged)       │
└───────────────────────────────┘
```

## Risks

| Risk                                                              | Mitigation                                                                                         |
| ----------------------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| Caller bypasses Session and calls VFS directly with relative path | `AbsolutePathsOnly` guard in VFS raises `ValueError` immediately                                   |
| Concurrent Session mutation of `cwd`                              | Session is not thread-safe by design; one Session per agent coroutine                              |
| `cd` permission check races with permission revocation            | VFS permission check on the subsequent operation is the authoritative gate; `cd` check is advisory |
