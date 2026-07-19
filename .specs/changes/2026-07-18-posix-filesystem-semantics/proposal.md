# Proposal: Broader POSIX filesystem semantics — DRAFT / UNRESOLVED

> Status: **parking lot**, not scoped, not ready to implement. Captured so the idea isn't lost;
> it is "a whole 'nother discussion for a different time." Do not implement from this document —
> it only enumerates the gap and the option space.

## Intent

The VFS deliberately exposes a **whole-file, path-based** interface (`FsPort`: `read`/`write`/`list`/`stat`/`exists`/`delete`/`mkdir`).
Several POSIX filesystem operations are therefore absent or degraded.
Code-mode (Monty over the VFS via `MontyVfsOS`) makes this visible: an agent writing ordinary Python can call `Path.rename`, `Path.mkdir`, `os.chmod`, etc., and hit operations the VFS doesn't fully support.
This proposal is to **decide, as its own change, which POSIX semantics to add** — not to add them now.

Related driver: the code-mode direction (`.specs/changes/…`/demo) surfaced these divergences during the `pydantic-ai-harness` `CodeMode(os_access=MontyVfsOS)` integration.
Existing `TODO.md` already lists a subset (mkdir/rmdir, file modes / executable bit, symlinks) and points at `._scratch/vfs-git-hosting-feasibility.md`.

## Current state (what's missing or degraded)

Observed against `src/vfs/execution/monty_os.py` (`MontyVfsOS`) and `src/vfs/execution/fs_port.py` (`SessionFsPort`):

| Operation                                     | Today                                              | Why                                                                                                         |
| --------------------------------------------- | -------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| `move` / `rename`                             | **raises** `UnsupportedOperationError`             | `FsPort` has no `move`; whole-file interface boundary. `Session.move`/`vfs.move` exist outside the sandbox. |
| `mkdir`                                       | no-op-ish (delegates to `fs.mkdir`, implicit dirs) | flat path namespace has no directory objects                                                                |
| `rmdir`                                       | no-op                                              | directories are implicit prefixes                                                                           |
| symlinks (`symlink`/`readlink`, `is_symlink`) | unsupported / always `False`                       | VFS has no link concept                                                                                     |
| `chmod` / file modes / executable bit         | unsupported (`NoReturn`)                           | no permission-bit metadata on files                                                                         |
| `utime` (mtime/atime)                         | unsupported (`NoReturn`)                           | timestamps are version metadata, not mutable file attrs                                                     |
| `append`                                      | works via read-modify-write                        | no native append primitive                                                                                  |

## Option space (to resolve later)

- **`move`/`rename`** — cheapest win: either emulate (`read`+`write`+`delete`) in `MontyVfsOS`, or thread `Session.move` through `FsPort` as a first-class op.
  Decide whether rename is a whole-file op the port should own.
- **Real directories** — requires directory objects in the metadata store (vs today's implicit prefixes).
  Large model change; affects `list`, `mkdir`/`rmdir`, empty-dir semantics.
- **Symlinks** — a link node type + resolution rules + cycle handling.
  Interacts with permissions and audit.
- **File modes / executable bit / `chmod`** — mode metadata on `FileMeta`/`VersionMeta`; decide whether it's versioned.
  Ties to any future "run this file" semantics.
- **`utime`** — likely reject permanently (timestamps are derived version metadata); document as intentionally unsupported rather than a gap.

## Open questions

- Which of these does the VFS's north star actually want, vs. deliberately reject?
  (Objects-vs-files boundary — cf. the S3 "Files" note in `TODO.md`.)
- Any change here touches data schemas/contracts → migration plan + artifact version bump per governance.
- Scope against real need: code-mode ergonomics (rename, mkdir) may justify a narrow slice; symlinks/modes may not.

## Non-goals (for now)

Implementing any of the above.
This is a reminder + option map only.
