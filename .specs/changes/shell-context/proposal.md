# Shell Context: Session, CWD, and Relative Paths

**Change name:** `shell-context` **Date:** 2026-04-04 **Author:** ahgraber + Claude

## Intent

Agents interact with the VFS using bash-style interfaces.
Relative paths (e.g., `../sibling/file.py`, `./notes.md`) are natural in that paradigm but currently undefined — the VFS only speaks absolute paths.

This change adds a `Session` layer that tracks per-session CWD state and resolves relative paths to absolute before they reach the VFS core.
It also hardens the VFS boundary to explicitly reject any non-absolute path, making the contract unambiguous.

## Scope

### In Scope

- **`Session` class**: holds `namespace_id`, `principal_id`, and `cwd` (default `"/"`)
- **Relative path resolution**: POSIX join + normpath applied before every VFS call
- **Traversal safety**: paths above `/` are impossible by construction (normpath clamps at root)
- **`cd(path)`**: resolves path, validates principal has read permission on the target, updates `cwd`
- **`pwd()`**: returns current `cwd` string
- **Session proxies all VFS operations**: `read`, `write`, `delete`, `stat`, `list`, `search`,
  `versions`, `rollback`, `copy`, `move` — all resolve path arguments through `cwd` before calling VFS
- **VFS boundary guard**: VFS raises `ValueError` on any non-absolute path argument

### Out of Scope

- Persistent CWD (session state is ephemeral — resets to `"/"` on construction)
- Shell history, environment variables, tab completion
- Symlinks
- Cross-namespace `cd`

## Approach

1. Add `AbsolutePathsOnly` requirement to `file-operations` and a one-line guard in VFS methods.
2. Implement `resolve_path(cwd, input_path) -> str` as a pure utility function (join → normpath).
3. Implement `Session` wrapping `VFS`, exposing the same interface with path resolution pre-applied.
4. `cd` checks read permission via a lightweight permission lookup, then updates `self.cwd`.

## Open Questions

- **Persistent CWD**: Should CWD survive agent reconnection?
  Currently out of scope (ephemeral).
  Could be added by storing CWD in principal metadata.
- **Strict `cd`**: Should `cd("/empty/prefix/")` fail if no files exist under that prefix?
  Current decision: permissive — VFS directories are implicit (path prefixes), so checking existence requires a list operation whose semantics are confusing.
  Permission check alone is sufficient.
