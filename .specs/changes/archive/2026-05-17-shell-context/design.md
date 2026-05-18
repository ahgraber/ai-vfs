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
    result = posixpath.normpath(joined)
    if joined.endswith("/") and result != "/":
        result += "/"
    return result
```

`normpath` handles `..`, `.`, duplicate slashes, and clamps traversal above `/`
(`normpath("/../x") == "/x"`), so no additional guard is needed.

A trailing `/` on the joined input is preserved post-normalization: `normpath` strips trailing slashes, but the VFS `list` and `search` prefix-matching (SQL `LIKE 'prefix%'` plus a `remainder` split on `/`) relies on a trailing slash to correctly delimit a directory prefix from a file path with the same leading characters.
Without this, `session.list("src/")` from `cwd="/workspace/"` would resolve to `"/workspace/src"` and the non-recursive filter would reject every match because `remainder` would always begin with `/`.
Preserving the input's trailing slash keeps the `AllPathArgsResolved` and `CdAbsolute` spec scenarios truthful.

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

### D5: Session methods mirror VFS path/content arguments

**Rationale:** Session matches each VFS path-and-content keyword (path, src, dst, content, expected_version, version_number, recursive, limit, before, query, scope, search_type, target_version) so any code written in terms of those arguments can call either object.
The bound `namespace_id` and `principal_id` are not re-passed — Session owns them at construction — so Session is not a literal drop-in for callers that pass those arguments explicitly to VFS.
The mirror is for ergonomic compatibility with the planned shell-operations layer (see D6), not for substitutability of the full VFS surface.

---

### D6: `FsOperations` (Monty execution layer) is constructed against a Session, not VFS

**Forward reference:** The main design doc § 3.4 and § 3.5 describe an `FsOperations` dataclass injected into Monty as explicit external functions, and a Shell Operations Layer (`grep`, `cat`, `ls`, `cd`, `pwd`, `edit`, …) that wraps VFS in bash-familiar names.
Session is the **stateful bind point** for that layer: a `Session` carries the `(namespace_id, principal_id, cwd)` triple that `FsOperations` callbacks need.
The expected construction pattern is `FsOperations(cd=session.cd, pwd=session.pwd, read=session.read, …)` so each bash wrapper inherits Session's CWD resolution and permission scope.

**Rationale:** Without Session, `FsOperations` would either have to manage CWD itself (duplicating shell-context's logic per wrapper) or push CWD into VFS (which D1 explicitly rejects).
Putting CWD on Session and binding `FsOperations` against it keeps three concerns cleanly separated: VFS owns storage + permission gates, Session owns CWD + relative-path resolution, `FsOperations` owns bash naming + anchor management.

**Scope note:** Phase 1 of this change does not implement `FsOperations` — that lives in the execution-providers change.
This decision exists to make sure the Session API surface (signatures, return types, async semantics) is compatible with the planned wrapper layer.

---

### D7: `cd` normalizes targets as directory prefixes

**Rationale:** Permission grants store `path_prefix` values that are conventionally directory-style (trailing `/`), and `check_permission` matches via `path.startswith(prefix)`.
A cwd stored as `/workspace` (no trailing slash) would fail to match a `/workspace/` grant.
The `CdDotDot` spec scenario also explicitly asserts that `cd("..")` from `/workspace/src/` lands at `/workspace/` (with the trailing slash).
After `resolve_path` runs, `cd` appends `/` to the result if missing (except for root `/`, which is already its own marker).
This keeps the cwd shape consistent with the prefix-matching contract and with the spec's expectations regardless of whether the input had a trailing slash (`cd("..")`, `cd("workspace")` and `cd("/workspace/")` all land at the same normalized cwd).

`resolve_path` itself is left general-purpose — it preserves a trailing slash only when the input had one — because file-style arguments (`session.read("file.txt")`) and directory-style arguments (`session.list("src/")`) both flow through it and must round-trip correctly.

---

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
