# Tasks: Sandbox Filesystem Mount + Standalone Anchored Editing

> Ordered foundation-first: `anchored-editing` → FS-port → Monty mount → just-bash provider →
> registry/extras → consumer samples → docs. Each SHALL requirement is paired with an
> evidence-producing test. Monty/just-bash tests are `pytest.mark.skipif(not HAS_*, ...)`;
> wrap concurrent act phases with pyleak per the project testing rules.

## anchored-editing (capability foundation)

- [x] Implement the anchor primitive in a new `vfs/anchored_editing/` module:
  `anchor = f"{abs_line_index}:{ck}"`, `ck = blake3(f"{abs_line_index}\n{line_text}").hexdigest()[:3]` (k=3 default);
  pure functions (no I/O); checksum length `k` configurable.
- [x] Implement anchor resolution against a content buffer: parse `index:ck`, resolve the absolute
  index to a line, and verify the checksum matches `(index, line_text)`; report mismatch
  (fabrication/transposition/cross-file), out-of-range, or inverted (end before start).
- [x] Implement `read_anchored(path, offset=None, limit=None)` bound to a `(namespace, principal)`
  context: return content for the (optional) window, the file's current version, and per-line
  anchors carrying **absolute** indices; strict UTF-8 decode (undecodable → raise a structured
  decode error, no anchors); `\n`-split with `\r` retained; trailing-newline preserved.
- [x] Implement `edit_anchored(path, hunks, expected_version)` accepting **one or more** hunks: conflict if current version != `expected_version` (strict, before any write); else resolve each hunk and apply all atomically (any hunk failing to resolve aborts the whole edit), writing one new version.
  Agent-facing result is success/failure (+ new version) — no content, no anchors.
- [x] Implement conflict handling: version mismatch, checksum mismatch, out-of-range index,
  inverted range, or tombstoned target → conflict; never a partial or guessed-location write.
- [x] Add a named conflict error (`AnchorConflictError`) and establish one error convention: the
  capability raises typed exceptions (`AnchorConflictError`, `PermissionDeniedError`,
  `NotFoundError`, decode error); ensure it maps through `vfs.execute`'s translation table
  (`anchor_conflict`).
- [x] Wire `read_anchored`/`edit_anchored` to enforce the principal's read/write permissions via
  `Session`.
- [x] **Test** `AnchorIdentity`: an identical-boilerplate block is uniquely targetable by index
  (no ambiguity cliff); an anchor resolves in a separate call with no shared state; altering an
  anchor's index without recomputing the checksum is detectably inconsistent.
  (partition: boilerplate / cross-call / index-content binding)
- [x] **Test** `AnchoredRead`: returns content + version + N anchors; a windowed read
  (`offset=100`) emits **absolute** indices matching a full read; empty and single-line files
  handled with no error; undecodable content raises decode error with no anchors; CRLF and
  missing-trailing-newline preserved through a read→edit round-trip.
- [x] **Test** `AnchoredEdit`: single hunk replaces the range and writes V+1; multiple
  non-overlapping hunks apply atomically in one version; the result carries success + new version
  and **no content/anchors**.
- [x] **Test** `AnchoredEditConflicts`: version changed → conflict; **checksum mismatch**
  (index transposed to an adjacent identical line, or anchor from another file) → conflict, no
  wrong-line write; out-of-range index → conflict; inverted range → conflict; tombstoned file →
  fails. (partition: file-changed / checksum-mismatch / out-of-range / inverted / tombstone)
- [x] **Test** `ConsistencyFloor`: a simulated stale read whose edit commits against the
  authoritative version is rejected as a conflict (never applied to non-current content);
  two principals editing the same file at version V → exactly one succeeds, the other
  conflicts (neither silently lost).
- [x] **Test** `AnchoredEditingStandaloneSurface`: full `read_anchored`→`edit_anchored` cycle with
  no sandbox; `edit_anchored` without write permission → `PermissionDeniedError`.

## execution — FS-port (boundary contract)

- [x] Define the FS-port interface in `vfs/protocols/` (sibling to metadata/blob/search):
  async `read`/`write`/`list`/`stat`/`exists`/`delete` + `mkdir` (no-op).
- [x] Implement the session-backed FS-port: every method routes through `Session` (permissions
  enforced, state-changing ops audited); unsupported ops (`symlink`/`chmod`/`utimes`) raise a
  clear unsupported-operation error; never touches the host filesystem.
- [x] **Test** `FsPortContract`: read/write round-trip through session with permission checks;
  unauthorized path → `PermissionDeniedError`; `mkdir` no-op then write-under-prefix works;
  unsupported op raises; a host path is not reachable.

## execution — Monty native mount

- [x] Implement a `MontyVfsOS(AbstractOS)` adapter whose `__call__`/`dispatch` maps Monty's `Path.*`/`Open` operations onto the FS-port, bridging each sync callback via `asyncio.run_coroutine_threadsafe(coro, host_loop).result()`.
  The adapter carries its own VFS-error sentinel so a `PermissionDeniedError`/`NotFoundError` survives Monty's downcast.
  Docstring states: interpreter-level virtual filesystem, a proxy into the governed VFS, not an OS/FUSE mount.
- [x] Update `MontyExecutionProvider.execute` to pass `os=MontyVfsOS(...)` to `run_async`
  alongside the existing `external_functions` (verbs kept additively).
- [x] Update `fs_operations_for` to drop the `anchor_map` parameter; update `vfs.execute` to stop
  constructing an `AnchorMap` and to construct/mount the FS-port the provider uses.
- [x] Update `ShellOperationsLayer` wiring: `cat`/`head`/`tail` compute content-derived anchors
  via `anchored-editing` (not the removed `AnchorMap`); `write` no longer invalidates anchors;
  `edit` delegates to `anchored-editing.edit_anchored`.
- [x] Remove the `AnchorMap`/token-pool implementation now superseded by stateless anchors.
- [x] **Test** `FsOperationsFactory` / `VfsExecutePermission`: `fs_operations_for` works without an
  `anchor_map` arg; `vfs.execute` constructs no `AnchorMap`; the three carried-over scenarios
  (relative-path resolve, budget overflow, fresh-counter / execute-permission) still pass.
- [x] **Test** `MontyNativeFilesystemMount` (skipif no monty): native `open`/`pathlib` read returns
  VFS content; native write persists a version; unauthorized native read denied AND surfaces
  `error_type="permission_denied"` through `vfs.execute` (mount-path error preservation); host
  path unreachable; heartbeat keeps ticking during native FS calls (`no_thread_leaks` around
  the act phase — worker-thread bridge).
- [x] **Test** `MontyProviderIntegration` (skipif no monty): simple expression output; native FS
  access without an injected verb; `grep` via `external_functions` reaches `session.search`;
  Monty-internal timeout → `provider_error` (no host path); `edit` from sandbox modifies the
  file via `anchored-editing`.
- [x] **Test** `ShellOperationsLayer` (skipif no monty): `cat` returns content + content-derived
  anchors; `edit` round-trips through `anchored-editing`; `grep` propagates
  `ReindexRequiredError`.

## execution — just-bash provider

- [x] **Spike (blocking, before implementing):** resolve the `commands=`/`cat`-returns-empty interaction observed in the design spike, confirm whether `commands=` merges or replaces the builtin registry, and determine how an overridden command resolves the working directory / scope (`CommandContext` exposes neither `fs` nor `cwd`).
  Record the resolution in `design.md`.
- [x] Implement an `IFileSystem` adapter over the FS-port (async passthrough); unsupported POSIX
  ops raise.
- [x] Implement `grep`/`find`/`glob` `Command` overrides whose async `execute(args, ctx)` call
  `session.search` (REGEX/FIND/GLOB) and format `ExecResult`; close over the session.
- [x] Implement `JustBashExecutionProvider` (`execute`/`capabilities`/`reset`) constructing
  `Bash(fs=..., commands=...)` and returning an `ExecutionResult`.
- [x] **Test** `JustBashProvider` (skipif no just-bash; `no_task_leaks` around act): `cat` reads a
  VFS file; bash write (redirection) persists a version; overridden `grep` returns index-backed
  results (not brute-force); unauthorized `cat` denied.

## execution — registry & extras

- [ ] Register `monty` and `just-bash` providers in `resolve_execution_provider` with per-provider
  lazy imports and per-extra actionable install messages.
- [ ] Regenerate `uv.lock` for the extras move; add the `codemode` extra to the test/CI matrix so
  execution tests run.
- [ ] **Test** `ExecutionProviderRegistry`: unknown provider rejected; missing `monty` extra →
  actionable message; missing `just-bash` extra → actionable message;
  `VfsImportsWithoutAnyProvider` — import `vfs` and run a non-execute op with neither extra
  importable (monkeypatch the imports absent).

## consumers (non-normative, outside the library)

- [ ] Add a `examples/pydantic_ai_codemode.py` sample: register a `bash`/`execute` tool and the
  standalone anchored-edit tool over a mounted sandbox, using only public `vfs` surface.
- [ ] Add a thin `examples/langgraph_smoke.py` gut-check sketch (can it mount + edit?), labeled
  disposable; assert it imports nothing from `vfs` internals.
- [ ] **Test** (smoke): the pydantic-ai sample drives one execute + one anchored edit end-to-end
  against an in-memory VFS; the sample imports only public `vfs` names.

## docs & cleanup

- [ ] Docstrings on the FS-port, `MontyVfsOS`, and the just-bash adapter stating the
  proxy-not-OS-mount model and the permission/audit inheritance.
- [ ] Update stale Monty API prose in `docs/research/fsspec and tigerfs research.md` and
  `docs/research/sandboxed-execution-research-convo.md` (the pool-based `AsyncMonty()` snippet
  is unreleased; 0.0.18 uses `Monty(code).run_async(...)` + `os=`).
- [ ] Add a CHANGELOG `Unreleased` entry: native filesystem mount (Monty), just-bash provider,
  standalone stateless anchored editing, sandboxes moved to optional extras.
