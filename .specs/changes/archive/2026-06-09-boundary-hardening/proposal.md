# Boundary Hardening

**Change name:** `boundary-hardening` **Date:** 2026-06-09 **Author:** ahgraber + Claude

## Intent

Fix four security and correctness bugs confirmed in the current code that allow confused-deputy path traversal, segment-boundary permission bypass, SQL LIKE wildcard injection, and a concurrent write race that surfaces as a raw `IntegrityError`.
None of these require a new capability — all are hardening passes against existing contract promises.

**Prerequisite:** `phase2-storage` must be applied (its adapters are the targets of issues 2–4).

## Scope

### In Scope

1. **Canonical absolute paths at the VFS boundary** (`vfs.py`).
   Upgrade `_require_absolute` to `_require_canonical`: after stripping at most one trailing `/` (root `/` exempt), the path must equal `posixpath.normpath(path)`.
   Rejects `..`, `.` segments, and `//` forms before any permission check or storage access.
   Path-prefix arguments to `grant()` are validated the same way.

2. **Segment-aware permission prefix matching** (all `MetadataStore` adapters).
   Replace `path.startswith(path_prefix)` with a segment-boundary check so a grant on `/work` does not cover `/workspace/file`.
   A prefix P covers path X iff `X == P` or `X` starts with `P + "/"` (or `P` when P already ends with `/`).
   Validate that `path_prefix` is canonical at the `set_permission` / `grant` call sites.

3. **LIKE/regex wildcard escaping in prefix queries** (`BaseSqlMetadataStore`).
   `list_dir` builds a LIKE pattern from caller-supplied data without escaping `_` and `%`, so `/my_dir/` matches `/myXdir/`.
   Escape `\`, `%`, and `_` with an ESCAPE clause before every LIKE query built from caller data.
   Mongo's `$regex` prefix query already uses `re.escape`; confirm and leave it unchanged.

4. **Concurrent write without `expected_version`** (`VFS`, all `MetadataStore` adapters).
   Two concurrent no-CAS writers both read version N and both try to insert version N+1; the unique constraint on `(namespace_id, file_path, version_number)` turns one of them into a raw `IntegrityError` / `DuplicateKeyError`.
   Stores translate that violation into a new `VersionCollisionError` (distinct from `ConflictError`).
   `VFS.write`, `VFS.copy` (destination), and `VFS.move` (destination) retry the read-compute-put loop on `VersionCollisionError`, bounded at 5 attempts.

### Out of Scope

- Server-side de-duplication or locking for concurrent writes (YAGNI).
- Cross-namespace permission grants.
- Any other storage adapter changes (tier-based retention, FTS).

## Approach

1. Amend `vfs.py:_require_absolute` → `_require_canonical`; update all call sites.
2. Add `_prefix_matches` helper in each `MetadataStore` adapter; update
   `check_permission` and validate prefix at `set_permission`.
3. Add `_like_escape` to `BaseSqlMetadataStore`; apply to `list_dir`.
4. Add `VersionCollisionError` to `vfs/errors.py`; translate in all adapters;
   add retry loop in `VFS.write`, `VFS.copy`, and `VFS.move`.

## Open Questions

None.
