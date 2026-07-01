# Design: Sandbox Filesystem Mount

## Context

- ai-vfs is **async throughout** (`Session`/`VFS` ops are coroutines).
  Nothing in this change converts the core to sync; the only sync surface is the Monty mount bridge.
- Two sandbox surfaces are now available in the installed deps and drive the design:
  - **pydantic-monty 0.0.18** exposes a native filesystem mount via `Monty.run_async(os=...)`, where `os` is an `AbstractOS` whose `__call__(function_name, args, kwargs)` intercepts the sandbox's `open`/`pathlib`/`os` operations.
    Its file callbacks are **synchronous**.
  - **just-bash 0.2.1** exposes `Bash(fs=IFileSystem, commands={name: Command})`; `IFileSystem`
    is **fully async**, and `Command` is a Protocol with async
    `execute(args: list[str], ctx: CommandContext) -> ExecResult`.
- Editing in code-mode is done with native file I/O (Python `open`/`pathlib` in Monty, bash redirection in just-bash) through the **FS-port**, not via any anchored-edit capability.

> Hash-anchored editing and its read/edit freshness model are split to a separate future change at `.specs/changes/2026-06-30-anchored-editing/`; that design space is unresolved and is not carried here.

## Decisions

### Decision: FsPortAsWeakestCommonDenominator

**Chosen:** One internal async FS-port — `read`/`write`/`list`/`stat`/`exists`/`delete` + `mkdir`-noop, every method routed through `Session` — sits at the boundary between the VFS layer and the execution-environment layer.
Monty's `AbstractOS` and just-bash's `IFileSystem` are **adapters** onto it.
POSIX operations above the floor (`symlink`/`chmod`/`utimes`) raise unsupported.

**Rationale:** Same contract-floor / LSP discipline as `MetadataStore`/`BlobStore`: define the intersection of what every sandbox needs and what the VFS can govern, expose nothing only one side supports.
Governance (permissions, audit) lives in `Session` beneath the port, so no adapter can bypass it.

**Alternatives considered:**

- **fsspec as the bridge:** rejected — at the sandbox boundary fsspec only subtracts (loses
  CAS, op-budget, permission pruning, search routing) and adds a streaming half neither sandbox
  can use. fsspec's legitimate altitude is _below_ the blob store, out of scope here.
- **Per-sandbox bespoke filesystem code:** rejected — duplicates governance wiring per sandbox
  and has no shared substitutability contract.

### Decision: NativeWriteIsTheEditingPath

**Chosen:** Editing in code-mode goes through native write on the mount — `open(path, "w").write(...)` / `pathlib.Path.write_text` in Monty, redirection in just-bash — which routes through the FS-port to `session.write`.
There is no `edit` verb and no anchored read/edit surface in this change.

**Rationale:** Native I/O is the idiom sandboxed code already expects, and it inherits the FS-port's permission/audit/CAS guarantees for free.
Making `open(path, "w")` work end-to-end required the Monty mount adapter to implement `path_append_text`/`path_append_bytes` on the `AbstractOS`; without those callbacks the sandbox's write path raised `PermissionError`.
Native-mount writes are last-writer-wins (no version stamp on `open(...).write()`); compare-and-swap semantics for code-mode editing are out of scope and tracked with the deferred anchored-editing change.

**Dropped scenario (deliberate):** the baseline `MontyProviderIntegration` scenario `WriteFromSandboxWorks` (an injected `edit`/`write` verb persisting a version) is replaced by `NativeFilesystemAccessFromSandbox`, which exercises native read **and** write (including `open(path, "w").write(...)`) through the mount.
Marked in the delta with `<!-- modified-removes: WriteFromSandboxWorks -->`.

### Decision: KeepInjectedVerbsAdditive

**Chosen:** Add the native mount **alongside** the existing injected verbs on Monty; do not shrink the verb set.

**Rationale:** The injected verbs carry agent affordances the native mount cannot express — search-index-routed `grep`/`find`/`glob` and structured `ls` metadata.
They are not redundant with `open`/`pathlib`.
Additive is minimal-scope and breaks no existing in-Monty workflow.
A later consolidation is a separate decision.

### Decision: MontyMountViaThreadsafeBridge

**Chosen:** Mount the FS-port as Monty's `os=` `AbstractOS`.
Each synchronous `AbstractOS` callback drives the async FS-port via `asyncio.run_coroutine_threadsafe(coro, host_loop).result()`.

**Rationale (spike-verified):** Monty dispatches `os` callbacks on a **worker thread**, not the host loop thread (spike: `same_thread = False`, `bridge_ok = True`, sandbox `Path('/x').read_text()` returned a value produced by an async coroutine).
Because the callback is off-loop, `run_coroutine_threadsafe(...).result()` blocks only the worker thread while the coroutine runs on the host loop — no re-entrancy, no loop starvation, no core change.

**Error preservation through the mount (was a review finding).**
`monty_provider`'s existing `_wrap` sentinel preserves VFS-error identity through Monty's exception downcast only for `external_functions`.
The `os=` mount is a **separate** surface, so a `PermissionDeniedError`/`NotFoundError` raised in a bridged callback would otherwise downcast to a generic exception and surface as `provider_error`/`internal_error`.
The `MontyVfsOS` adapter therefore carries its own error-capture (the same sentinel pattern), so `vfs.execute`'s translation table maps mount-path VFS errors to their real `error_type` (covered by `NativeMountDenialTranslatesToPermissionDenied`).

**Bridge concurrency (single-flight, was a review finding).**
A Monty sandbox runs one sequential program (the subset has no threading), so its native FS callbacks do not overlap — each blocks the worker, returns, then the next runs.
The per-`execute` `Session` is therefore single-flight; no cross-call `Session` coroutine-safety is required.
If a future provider runs concurrent guest tasks, this assumption must be revisited.

**Alternatives considered:**

- Async `os` callbacks: not offered by 0.0.18 (the `os` surface is sync; only
  `external_functions` are awaited).
- Running VFS ops synchronously: rejected — would mean a sync VFS or blocking the loop.

### Decision: JustBashAsyncPassthroughPlusCommandOverride

**Chosen:** Provide `Bash(fs=<FS-port adapter>)` for native bash file I/O (async passthrough,
no bridge), and override `grep`/`find`/`glob` via `commands={...}` with `Command` objects that
call `session.search`, so those resolve to the VFS search index (parity with Monty).

**Rationale (spike-verified):** `IFileSystem` is fully async, so bash builtins call our async session adapter directly.
`commands=` overrides named builtins; an overridden `grep` is routed to our `Command.execute`.
The override closes over the `session` so scope resolves against the session's `cwd`.

**Spike resolution (the blocking just-bash questions, settled against the installed package):**

- **`commands=` _replaces_ the builtin registry — it does not merge.**
  Passing `commands={"grep": ...}` alone leaves `cat`/`echo`/`ls` reporting `command not found` (exit 127).
  This is the real cause of the earlier "`cat` returns empty" anomaly.
  The provider therefore builds the full builtin registry via `just_bash.commands.create_command_registry()` (87 commands) and overrides `grep`/`find`/`glob` on top of it.
- **`CommandContext` _does_ expose `cwd` and `fs` at runtime** (instance attributes, not class-level — so they don't appear in `dir(CommandContext)` but are present on the live object).
  The overridden commands resolve a relative scope argument against `ctx.cwd`; they still close over the `session` for the actual search call.
- **`glob` is not a bash builtin** (globbing is shell expansion).
  We add a `glob` command that routes to `session.search(GLOB)` for parity with the Monty verbs; `grep`/`find` override existing builtins.
- **Installed version:** the distribution is `just-bash==0.2.1` (matches the pin and `uv.lock`); the package's `__version__` string is stale ("0.1.0") and is not authoritative.
  The `IFileSystem` surface is the larger 0.2.1 method set (`read_file`/`read_file_bytes`/`write_file`/`append_file`/`exists`/`is_file`/`is_directory`/ `readdir`/`mkdir`/`rm`/`stat`/`resolve_path`/… plus `chmod`/`symlink`/`readlink`/`utimes`, which raise unsupported).

**Alternatives considered:**

- Let just-bash's native `grep`/`find` brute-force over the `fs` adapter: correct but bypasses
  the search index and the `SearchLimits` budget; rejected in favor of index parity (decision
  `b` from review).

### Decision: SandboxesAsOptionalExtras

**Chosen:** `monty` and `just-bash` are granular optional extras plus a `codemode` umbrella
(`ai-vfs[monty,just-bash]`); the VFS layer installs and runs standalone.

**Rationale:** Matches the north star's "in-process interpreters are the default profile; heavier sandboxes optional," and the existing per-adapter extra pattern (`postgres`/`mongo`/`s3`).
The lazy `resolve_execution_provider` raises an actionable install hint per provider.

## Architecture

```text
storage backends  (SQLite / Postgres / Mongo / S3 / local FS)
   ▲  MetadataStore / BlobStore protocols            ── boundary #1 (exists)
VFS core layer    read/write/list/search · permissions · audit · versioning · CAS   [async]
   │
   │ (Session)
   ▼
FS-port  (async; read/write/list/stat/exists/delete + mkdir-noop)
   ▲  ── boundary #2 (this change)
   │
   ┌───────┴────────────────────────┐
   │                                │
Monty AbstractOS adapter        just-bash IFileSystem adapter
(sync callback →                (async passthrough)
 run_coroutine_threadsafe)      + grep/find/glob via commands= → session.search
```

- The mount is an **interpreter-level virtual filesystem** — a proxy into the governed VFS.
  No FUSE, no OS mount, no host filesystem exposure.
- Native-mount writes are last-writer-wins (no version stamp on `open(...).write()`); making `open(path, "w")` work required the Monty adapter to implement `path_append_text`/`path_append_bytes`.
  Permissions + audit hold on every op.

## Risks

- **Worker-thread parking (Monty bridge):** each in-flight native FS call blocks one Monty worker thread until the host-loop coroutine completes.
  Fine for serial-IO sandboxes; under heavy parallel FS fan-out it bounds throughput.
  Mitigation: document; revisit a bounded worker pool only if profiling shows contention.
- **just-bash maturity:** 0.2.1 is pre-release and a third-party port.
  Mitigation: pin the version, own a conformance test over the `fs=` adapter and the overridden commands, and treat the provider as optional (extra).
- **just-bash `commands=` semantics unverified at edges:** the spike showed override works but also a `cat`-returns-empty anomaly after `commands=` and that `CommandContext` exposes neither `fs` nor `cwd`.
  Mitigation: at implementation, confirm merge-vs-replace of the registry and how the overridden command resolves the working directory / scope (likely threaded via the session adapter, not `ctx`).
- **Cross-store revive race:** pre-existing and accepted at PoC scale (storage spec); orthogonal
  to this change.

## Review Dispositions

Findings from the fresh-eyes review that were **declined**, recorded for the audit trail:

- **"`FsPortContract` over-specifies an internal interface by listing its methods."**
  Declined — the FS-port is a protocol boundary that future sandbox providers depend on, exactly like `MetadataStore`/`BlobStore`, which the baseline storage spec enumerates _with their methods as contract_.
  Enumerating the FS-port methods is consistent with that precedent, not a §1.4 violation.

## Open Questions (non-blocking)

- LangGraph gut-check depth — keep it a thin "can it mount, read, and write?"
  sketch; it exists to falsify coupling, not to ship a LangGraph integration.
  </content>
