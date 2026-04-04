# Shell Context — Tasks

> **Implementation note:** Use `superpowers:test-driven-development` skill when implementing.
> Each task follows Red → Green → Commit.
> Run tests with: `uv run pytest tests/` (all) or `uv run pytest -n auto tests/unit/` (parallel unit tests).

---

## Group 1: VFS Boundary Guard

### Task 1: AbsolutePathsOnly guard in VFS

**Files:**

- Modify: `src/vfs/vfs.py`
- Modify: `tests/integration/test_vfs_file_operations.py`

**Spec refs:** `file-operations/AbsolutePathsOnly`

- [ ] Add tests:

  - `test_relative_path_raises_valueerror`: call each VFS method (`read`, `write`, `delete`,
    `stat`, `list`) with a relative path → `ValueError` raised
  - `test_absolute_path_accepted`: call each method with an absolute path → no `ValueError`
    at the boundary (normal permission/not-found errors may follow)

- [ ] Run tests — confirm they fail

- [ ] Add a `_require_absolute(path: str) -> None` helper in `vfs.py`:

  ```python
  def _require_absolute(path: str) -> None:
      if not path.startswith("/"):
          raise ValueError(f"path must be absolute, got {path!r}")
  ```

Call at the top of each VFS method that accepts a `path` argument.
For `copy` and `move`, guard both `src` and `dst`.

- [ ] Run tests — confirm they pass

- [ ] Commit: `feat(vfs): reject non-absolute paths at VFS boundary`

---

## Group 2: Path Resolution Utility

### Task 2: `resolve_path` utility function

**Files:**

- Create: `src/vfs/session.py` (utility function only, class in next task)
- Create: `tests/unit/test_session.py`

**Spec refs:** `session/RelativePathResolution`, `session/PathTraversalPrevention`

- [ ] Write `tests/unit/test_session.py` — `resolve_path` tests:

  - `test_absolute_path_unchanged`: `resolve_path("/src/", "/data/file.txt")` → `"/data/file.txt"`
  - `test_relative_path_joined`: `resolve_path("/src/", "utils.py")` → `"/src/utils.py"`
  - `test_dot_path_resolved`: `resolve_path("/src/app/", "./config.py")` → `"/src/app/config.py"`
  - `test_dotdot_resolved`: `resolve_path("/src/app/", "../lib.py")` → `"/src/lib.py"`
  - `test_dotdot_at_root_clamped`: `resolve_path("/", "../etc/passwd")` → `"/etc/passwd"`
  - `test_deep_traversal_clamped`: `resolve_path("/workspace/", "../../../../etc/passwd")` → `"/etc/passwd"`
  - `test_double_slash_normalized`: `resolve_path("/src//", "file.py")` → `"/src/file.py"`

- [ ] Run tests — confirm they fail

- [ ] Implement `resolve_path` in `src/vfs/session.py`:

  ```python
  import posixpath


  def resolve_path(cwd: str, path: str) -> str:
      if posixpath.isabs(path):
          joined = path
      else:
          joined = posixpath.join(cwd, path)
      return posixpath.normpath(joined)
  ```

- [ ] Run tests — confirm they pass

- [ ] Commit: `feat(vfs): add resolve_path utility with POSIX normalization`

---

## Group 3: Session Class

### Task 3: Session — construction, `pwd`, `cd`

**Files:**

- Modify: `src/vfs/session.py`
- Modify: `tests/unit/test_session.py`

**Spec refs:** `session/CWDState`, `session/CdOperation`, `session/PwdOperation`

- [ ] Add tests:

  - `test_default_cwd`: `Session(vfs, ns, principal)` → `pwd()` returns `"/"`
  - `test_pwd_returns_cwd`: after `cd("/workspace/")` → `pwd()` returns `"/workspace/"`
  - `test_cd_absolute`: `cd("/workspace/")` → `cwd` updated
  - `test_cd_relative`: `cwd="/workspace/"`, `cd("src/")` → `cwd` is `"/workspace/src"`
  - `test_cd_dotdot`: `cwd="/workspace/src/"`, `cd("..")` → `cwd` is `"/workspace"`
  - `test_cd_at_root_stays_root`: `cwd="/"`, `cd("..")` → `cwd` is `"/"`
  - `test_cd_permission_denied`: principal has no read on target → `PermissionDeniedError`,
    `cwd` unchanged (use mock VFS with `check_permission` returning `False`)
  - `test_cd_updates_only_on_success`: ensure atomicity — `cwd` is not mutated before
    the permission check resolves

- [ ] Run tests — confirm they fail

- [ ] Implement `Session` in `src/vfs/session.py`:

  ```python
  class Session:
      def __init__(self, vfs: VFS, namespace_id: str, principal_id: str) -> None:
          self._vfs = vfs
          self._namespace_id = namespace_id
          self._principal_id = principal_id
          self._cwd: str = "/"

      def pwd(self) -> str:
          return self._cwd

      async def cd(self, path: str) -> None:
          resolved = resolve_path(self._cwd, path)
          # Advisory permission check — raises PermissionDeniedError if denied
          await self._vfs._check_perm(self._principal_id, self._namespace_id, resolved, "read")
          self._cwd = resolved
  ```

Note: `_check_perm` is the internal permission helper already present in `VFS`.
If it is not exposed, call `vfs.stat(namespace_id, resolved, principal_id)` as a proxy (stat requires read permission and is a lightweight metadata-only call).

- [ ] Run tests — confirm they pass

- [ ] Commit: `feat(vfs): add Session with CWD state, cd, and pwd`

---

### Task 4: Session — proxy file operations

**Files:**

- Modify: `src/vfs/session.py`
- Create: `tests/integration/test_session_operations.py`

**Spec refs:** `session/SessionProxiesVFS`

- [ ] Write `tests/integration/test_session_operations.py` using a real VFS fixture:

  - `test_session_read_relative`: write file at `/workspace/file.txt`; session with
    `cwd="/workspace/"` reads `"file.txt"` → correct content returned
  - `test_session_write_relative`: session writes `"output.txt"` with `cwd="/workspace/"` →
    file appears at `/workspace/output.txt`
  - `test_session_list_relative`: files under `/workspace/src/`; `list("src/")` → correct results
  - `test_session_stat_relative`: stat `"src/main.py"` with `cwd="/workspace/"` →
    stat result matches `/workspace/src/main.py`
  - `test_session_delete_relative`: delete `"old.txt"` → tombstones `/workspace/old.txt`
  - `test_session_copy_relative_both`: copy `"a.py"` to `"../archive/a.py"` → correct src/dst
  - `test_session_move_relative_both`: move `"a.py"` to `"../archive/a.py"` → correct src/dst
  - `test_session_search_relative`: search `"*.py"` in `"src/"` → scoped to `/workspace/src/`
  - `test_session_versions_relative`: versions of `"file.txt"` → versions of `/workspace/file.txt`
  - `test_session_rollback_relative`: rollback `"file.txt"` to version 1 → rolls back correct path

- [ ] Run tests — confirm they fail

- [ ] Implement proxy methods on `Session`:

  ```python
  async def read(self, path: str, *, version_number: int | None = None) -> bytes:
      return await self._vfs.read(
          self._namespace_id, resolve_path(self._cwd, path), self._principal_id, version_number=version_number
      )


  async def write(self, path: str, content: bytes, *, expected_version: int | None = None):
      return await self._vfs.write(
          self._namespace_id,
          resolve_path(self._cwd, path),
          self._principal_id,
          content,
          expected_version=expected_version,
      )


  # ... stat, delete, list, search, versions, rollback, copy, move — same pattern
  ```

- [ ] Run tests — confirm they pass

- [ ] Commit: `feat(vfs): add Session proxy methods for all VFS operations`

---

### Task 5: Public API — export Session

**Files:**

- Modify: `src/vfs/__init__.py`

**Spec refs:** n/a (API hygiene)

- [ ] Add `Session` and `resolve_path` to `__init__.py` exports
- [ ] Commit: `feat(vfs): export Session from public API`

---

## Final: Coverage check

- [ ] Run full suite: `uv run pytest tests/`
- [ ] Verify each scenario in `session/spec.md` has a passing test
- [ ] Verify `file-operations/AbsolutePathsOnly` scenarios are covered
- [ ] Commit: `chore(vfs): shell-context complete — all tests passing`
