# Tasks: Sandbox Filesystem Mount

> Ordered foundation-first: FS-port → Monty mount → just-bash provider → registry/extras →
> consumer samples → docs. Each SHALL requirement is paired with an evidence-producing test.
> Monty/just-bash tests are `pytest.mark.skipif(not HAS_*, ...)`; wrap concurrent act phases
> with pyleak per the project testing rules.

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
  Implement the write path including `path_append_text`/`path_append_bytes` so `open(path, "w").write(...)` persists (it raised `PermissionError` without those callbacks).
  The adapter carries its own VFS-error sentinel so a `PermissionDeniedError`/`NotFoundError` survives Monty's downcast.
  Docstring states: interpreter-level virtual filesystem, a proxy into the governed VFS, not an OS/FUSE mount.
- [x] Update `MontyExecutionProvider.execute` to pass `os=MontyVfsOS(...)` to `run_async`
  alongside the existing `external_functions` (verbs kept additively).
- [x] Update `vfs.execute` to construct/mount the FS-port the provider uses.
- [x] **Test** `FsOperationsFactory` / `VfsExecutePermission`: `fs_operations_for` constructs the
  shell wrappers; the three carried-over scenarios (relative-path resolve, budget overflow,
  fresh-counter / execute-permission) still pass.
- [x] **Test** `MontyNativeFilesystemMount` (skipif no monty): native `open`/`pathlib` read returns
  VFS content; **native write incl. `open(path, "w").write(...)`** (the append-callback path) and
  `pathlib.Path.write_text` persist a version; unauthorized native read denied AND surfaces
  `error_type="permission_denied"` through `vfs.execute` (mount-path error preservation); host
  path unreachable; heartbeat keeps ticking during native FS calls (`no_thread_leaks` around
  the act phase — worker-thread bridge).
- [x] **Test** `MontyProviderIntegration` (skipif no monty): simple expression output; native FS
  access (read and write) without an injected verb; `grep` via `external_functions` reaches
  `session.search`; Monty-internal timeout → `provider_error` (no host path).

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

- [x] Register `monty` and `just-bash` providers in `resolve_execution_provider` with per-provider
  lazy imports and per-extra actionable install messages.
- [x] Regenerate `uv.lock` for the extras move; add the `codemode` extra to the test/CI matrix so
  execution tests run. (No CI matrix exists in the repo — only the Nix devshell; the dev env
  already installs the sandbox extras, so execution tests run locally.)
- [x] **Test** `ExecutionProviderRegistry`: unknown provider rejected; missing `monty` extra →
  actionable message; missing `just-bash` extra → actionable message;
  `VfsImportsWithoutAnyProvider` — import `vfs` and run a non-execute op with neither extra
  importable (monkeypatch the imports absent).

## consumers (non-normative, outside the library)

- [x] Add a `notebooks/pydantic_ai_codemode.py` sample: register a `bash`/`execute` tool over a
  mounted sandbox that demonstrates execute + native write, using only public `vfs` surface.
- [x] Add a thin `notebooks/langgraph_smoke.py` gut-check sketch (can it mount + read + write?),
  labeled disposable; assert it imports nothing from `vfs` internals.
- [x] **Test** (smoke): the pydantic-ai sample drives one execute + one native write end-to-end
  against an in-memory VFS; the sample imports only public `vfs` names.

## docs & cleanup

- [x] Docstrings on the FS-port, `MontyVfsOS`, and the just-bash adapter stating the
  proxy-not-OS-mount model and the permission/audit inheritance.
- [x] Update stale Monty API prose in `docs/research/fsspec and tigerfs research.md` and
  `docs/research/sandboxed-execution-research-convo.md` (the sync `Monty(...).run(...)` snippet
  predates 0.0.18, which uses `Monty(code).run_async(...)` + `os=`).
- [x] Add a CHANGELOG `Unreleased` entry: native filesystem mount (Monty), just-bash provider,
  sandboxes moved to optional extras.
  </content>
