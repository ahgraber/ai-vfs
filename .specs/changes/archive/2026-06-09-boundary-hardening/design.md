# Design: Boundary Hardening

## Context

Four bugs confirmed against `feature/phase2`:

| #   | Location                                                                 | Bug                                                                       |
| --- | ------------------------------------------------------------------------ | ------------------------------------------------------------------------- |
| 1   | `vfs.py:_require_absolute`                                               | Only checks `startswith("/")` — `/../` traversal passes through           |
| 2   | `sql_metadata.py:check_permission`, `mongo_metadata.py:check_permission` | Naive `startswith` — `/work` grant covers `/workspace/`                   |
| 3   | `sql_metadata.py:list_dir`                                               | Unescaped LIKE — `_` and `%` in paths are wildcards                       |
| 4   | `vfs.py:write`, stores' `put_version`                                    | No-CAS concurrent write race → raw `IntegrityError` / `DuplicateKeyError` |

## Decisions

### Decision: Single `_require_canonical` replaces `_require_absolute` everywhere

The new guard is:

```python
def _require_canonical(path: str) -> None:
    if not path.startswith("/"):
        raise ValueError(...)
    check = path[:-1] if (path.endswith("/") and path != "/") else path
    if check != posixpath.normpath(check):
        raise ValueError(...)
```

This accepts `/`, `/foo`, and `/foo/` (directory-style), and rejects any path containing `..`, `.` segments, or `//`.
A single trailing slash is stripped for the normpath comparison to let Session forward directory-style paths (e.g. `cd` always appends `/`).

The same logic is used inline in `set_permission` and `grant` to validate `path_prefix`.
Rather than creating a shared utility module (over-engineering for three locations), a small helper function is inlined in `vfs.py` and each store module.

### Decision: `_prefix_matches` replaces `startswith` in permission checks

```python
def _prefix_matches(path_prefix: str, path: str) -> bool:
    if path == path_prefix:
        return True
    dir_prefix = path_prefix if path_prefix.endswith("/") else path_prefix + "/"
    return path.startswith(dir_prefix)
```

Handles all four grant shapes: exact file (`/work`), directory with slash (`/work/`), root (`/`), and single-file grant (`/config.yaml`).
Length-based ordering is unchanged.

### Decision: `_like_escape` with `ESCAPE '\\'` for SQL prefix queries

```python
def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
```

Applied to every `LIKE`-based query built from caller data.
The `ESCAPE "\\"` clause is passed to SQLAlchemy's `like()`, which is supported by both `aiosqlite` and `asyncpg`.
Mongo already uses `re.escape`; no change needed there.

### Decision: `VersionCollisionError` (not `ConflictError`) for no-CAS version collision

A new `VersionCollisionError(VFSError)` is added to `vfs/errors.py`.
It is:

- **Raised** by stores when `INSERT ... version_number` violates the unique constraint
  (`IntegrityError` in SQL, `DuplicateKeyError` in Mongo) with `expected_version=None`.
- **Retried** by `VFS.write/copy/move` (destination) up to 5 times; each retry
  re-reads the current version number so the next attempt uses N+2 if N+1 is taken.
- **Not retried** when `expected_version` is provided (the caller is doing CAS and owns
  the conflict semantics).
- **Distinct** from `ConflictError` (which signals an expected_version CAS mismatch and
  must never be retried).

For Mongo, the `DuplicateKeyError` on the version insert is translated in both the `expected_version=None` and `expected_version!=None` code paths, since both insert the version document first.
With `expected_version` set, the VFS does not retry, so the distinction does not affect behavior there.

The retry loop lives in the VFS layer (not the store) because it requires a re-read of
file metadata plus a new ULID, both of which are VFS concerns, not store concerns.

## Architecture Impact

No new files.
Four surgical edits:

```text
src/vfs/errors.py                      +VersionCollisionError
src/vfs/vfs.py                         _require_canonical, retry loops, grant validation
src/vfs/stores/sql_metadata.py         _like_escape, _prefix_matches, VersionCollisionError
src/vfs/stores/mongo_metadata.py       _prefix_matches, VersionCollisionError
```

## Risks

- **LIKE ESCAPE dialect support**: SQLAlchemy's `column.like(pattern, escape=char)` maps to `LIKE ... ESCAPE ...` on both SQLite and Postgres.
  No risk.
- **Retry storms**: bounded at 5 attempts; in practice, a two-writer race resolves in 2 attempts.
  Exhausting 5 retries indicates a pathological workload, where the raised `VersionCollisionError` is the correct signal.
- **Session path invariant**: `Session.resolve_path` already runs `posixpath.normpath`;
  a test verifies it always produces canonical output.

## Verification

All requirements are covered by unit tests in `tests/unit/test_boundary_hardening.py`.
Integration tests for Mongo/Postgres collision behavior are in `tests/integration/test_boundary_hardening.py` (require Docker fixtures; marked accordingly).
