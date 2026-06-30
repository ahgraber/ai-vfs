# Proposal: Sandbox Filesystem Mount

## Intent

Today an agent's sandboxed code reaches its VFS files only through injected named functions (`cat("/x")`, `write("/x", data)`); it cannot use idiomatic file I/O. pydantic-monty 0.0.18 adds a native filesystem-mount surface (`os=AbstractOS`) that intercepts the sandbox's `open()`/`pathlib`/`os` calls, and just-bash 0.2.1 exposes an injectable async filesystem (`fs=`) plus overridable commands (`commands=`).
This change lets sandboxed code work over the **governed** VFS through native filesystem idioms — Python `open`/`pathlib` in Monty, bash builtins in just-bash — via one internal filesystem contract (the **FS-port**) that enforces the VFS's permission, audit, and concurrency guarantees.
Editing in code-mode is done with native file I/O — `open(path, "w").write(...)` / `pathlib` in Monty, bash redirection in just-bash — not via any anchored-edit capability.
It realizes the north star's bet #3 (filesystem interface, code-mode interaction) and bet #4 (portable, multi-sandbox) without weakening bet #2 (trust).

## User Stories

### Story: monty-code-mode

As a builder embedding an agent, I want the agent to run arbitrary Python over its VFS
through a natively mounted filesystem — with native `open`/`pathlib` read and write and
index-backed `grep`/`find`/`glob` available in-language — so that multi-step work composes
inside one sandboxed program instead of many tool round-trips.

### Story: just-bash-shell-tool

As a builder, I want to offer the agent a familiar Bash tool over its VFS — with native
read/write (redirection) and index-backed `grep`/`find`/`glob` — for exploration and
shell-style composition, so that the agent gets ergonomic shell access over the governed VFS.

### Story: governed-mount

As an operator, I want the mounted filesystem to enforce the same permissions, audit trail,
and optimistic-concurrency guarantees as direct VFS calls, so that native filesystem
ergonomics never become a path around governance.

### Story: portable-sandboxes

As a builder, I want Monty and just-bash to run over the same VFS through a single internal
filesystem contract, so that I am not locked to one execution engine and can add others
without touching the substrate.

## Scope

One capability: **execution** — the mount and providers that let sandboxed code read and
write the governed VFS through native filesystem idioms.

**In scope — execution:**

- An internal **FS-port** contract: an async, whole-file, path-based filesystem interface
  backed by `Session` that enforces permission checks, audit, and CAS — the boundary between
  the VFS layer and the execution-environment layer.
- **Monty native mount**: an `AbstractOS` adapter routing the sandbox's intercepted `open`/`pathlib`/`os` path operations to the FS-port, with a sync→async bridge (Monty dispatches FS callbacks off the host loop — verified by spike).
  The existing injected verbs are **kept additively** — native I/O is added alongside `cat`/`ls`/`grep`/`find`.
- **just-bash provider**: injects an FS-port-backed `IFileSystem` (`fs=`) and overrides
  `grep`/`find`/`glob` (via `commands=`) to route to `session.search` for index-backed
  acceleration (parity with Monty).
- **Dependency layering**: `monty`/`just-bash` as granular optional extras plus a `codemode`
  umbrella; the VFS layer installs standalone (pyproject done; lockfile + CI gating follow-on).
- **Mount-model documentation**: specs and adapter docstrings state that the mount is an
  interpreter-level virtual-filesystem **proxy into the governed VFS**, not an OS/FUSE mount;
  the host filesystem is never exposed to the sandbox.
- **Reconcile stale references**: update the stale Monty API prose in the research docs.
- A **pydantic-ai sample consumer** and a disposable **LangGraph gut-check sketch**
  (non-normative, outside the library) that drive `vfs.execute` over a mounted sandbox using
  only the public surface — evidence the surface is framework-agnostic.

**Out of scope:**

- Hash-anchored editing — split out to a separate future change at `.specs/changes/2026-06-30-anchored-editing/`; its design space is unresolved.
  Editing in code-mode is done with native file I/O.
- Converting the VFS core to synchronous — it stays async; the bridge is the only sync surface.
- fsspec anything (blob backend or frontend) — deferred, separate concern.
- Upstream TypeScript just-bash — the Python `just-bash` package only.
- True streaming / seekable file handles — the mount is whole-file, matching the
  no-streaming substrate.
- POSIX surface beyond the VFS model — `symlink`/`readlink`/`chmod`/`utimes` raise a clear
  unsupported-operation error rather than silently no-op.
- Shrinking Monty's injected verb set — verbs are kept additively this change; any later
  consolidation is its own decision.
- Full agent-framework integrations and crewai.

## Approach

> Mechanism sandbox — formalized in `design.md`.

- **FS-port = weakest common denominator** of Monty's `AbstractOS` needs, just-bash's `IFileSystem`, and our `Session` ops: `read`/`write`/`list`/`stat`/`exists`/`delete` (plus `mkdir` as a prefix no-op).
  Every method calls `Session`, so permission/audit/CAS hold at the port; the host OS is never touched.
  POSIX extras just-bash declares (`symlink`/`chmod`/`utimes`) sit above the floor and raise unsupported.
- **Monty bridge**: Monty dispatches `os=` callbacks on a worker thread (spike: `same_thread: False`), so each sync callback drives the async FS-port via `asyncio.run_coroutine_threadsafe(coro, host_loop).result()`.
  No core change; one bounded concern — each in-flight FS call parks a worker thread (note for heavy fan-out).
- **just-bash adapter**: `IFileSystem` is fully async, so the adapter is a direct passthrough to
  the FS-port — no bridge (spike-verified). `grep`/`find`/`glob` are replaced via `commands=`
  with versions that call `session.search` (spike-verified).
- **Native-mount writes are last-writer-wins**: native `open(...).write()` carries no version stamp, so mount writes use the VFS default (last-writer-wins with bounded retry).
  Native write is the editing story — `open(path, "w").write(...)` routes through the FS-port's append/write path.
  Permissions and audit hold on every mount op regardless.
- **Spikes**: Monty threading model (done — bridge verified); just-bash `fs=` passthrough +
  `commands=` override (done — verified; one `commands=`/`cat` interaction to pin before
  implementing, see `design.md`).

## Open Questions

- **Consumer gut-check depth** — how thin the LangGraph sketch should be; it exists only to
  falsify coupling, not to ship a LangGraph integration.
  </content>
  </invoke>
