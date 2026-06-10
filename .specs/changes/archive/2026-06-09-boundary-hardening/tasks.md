# Tasks: Boundary Hardening

## Spec & Design

- [x] Write proposal.md
- [x] Write design.md
- [x] Write delta specs (file-operations, access-control, storage)

## Issue 1 — Canonical absolute paths

- [x] Add `_require_canonical` to `vfs/vfs.py`, replacing `_require_absolute`
- [x] Update all `_require_absolute` call sites in `vfs.py` to `_require_canonical`
- [x] Add path-prefix canonicity validation in `VFS.grant()`
- [x] Test: VFS operations reject non-canonical paths (`../`, `./`, `//`)
- [x] Test: trailing-slash and root paths are accepted
- [x] Test: `Session.resolve_path` output always satisfies the canonical rule

## Issue 2 — Segment-aware permission prefix matching

- [x] Add `_prefix_matches` helper in `sql_metadata.py`; update `check_permission`
- [x] Add `_prefix_matches` helper in `mongo_metadata.py`; update `check_permission`
- [x] Add canonical `path_prefix` validation in `BaseSqlMetadataStore.set_permission`
- [x] Add canonical `path_prefix` validation in `MongoMetadataStore.set_permission`
- [x] Test: grant on `/work` does not match `/workspace/file`
- [x] Test: grant on `/work` matches `/work/file` and `/work` exactly
- [x] Test: grant on `/workspace/` matches `/workspace/file`

## Issue 3 — LIKE wildcard escaping

- [x] Add `_like_escape` helper in `sql_metadata.py`
- [x] Apply `_like_escape` in `BaseSqlMetadataStore.list_dir`
- [x] Test: a path containing `_` is not matched by a sibling where `_` is replaced by another char
- [x] Test: a path containing `%` is found correctly by exact prefix listing

## Issue 4 — Concurrent write without `expected_version`

- [x] Add `VersionCollisionError` to `vfs/errors.py`
- [x] Translate `IntegrityError` → `VersionCollisionError` in `BaseSqlMetadataStore.put_version` (expected_version=None path)
- [x] Translate `DuplicateKeyError` → `VersionCollisionError` in `MongoMetadataStore.put_version` (both paths)
- [x] Add retry loop (5 attempts) in `VFS.write`
- [x] Add retry loop in `VFS.copy` (destination write)
- [x] Add retry loop in `VFS.move` (destination write)
- [x] Test: write retry succeeds when version collision is injected once
- [x] Test: VersionCollisionError propagates after retries exhausted
- [x] Test: ConflictError from expected_version CAS is not retried

## Run tests

- [x] `uv run pytest tests/unit/test_boundary_hardening.py -x -q`
- [x] `uv run pytest tests/unit -x -q` (regression gate)
