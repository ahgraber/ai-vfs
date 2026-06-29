# Proposal: Sandbox Filesystem Mount + Standalone Anchored Editing

## Intent

Today an agent's sandboxed code reaches its VFS files only through injected named functions (`cat("/x")`, `write("/x", data)`); it cannot use idiomatic file I/O, and hash-anchored editing is locked inside a single sandbox run. pydantic-monty 0.0.18 adds a native filesystem-mount surface (`os=AbstractOS`) that intercepts the sandbox's `open()`/`pathlib`/`os` calls, and just-bash 0.2.1 exposes an injectable async filesystem (`fs=`) plus overridable commands (`commands=`).
This change (1) lets sandboxed code work over the **governed** VFS through native filesystem idioms — Python `open`/`pathlib` in Monty, bash builtins in just-bash — via one internal filesystem contract (the **FS-port**) that enforces the VFS's permission, audit, and concurrency guarantees; and (2) promotes hash-anchored editing into a standalone, **stateless** capability any agent can call as a tool, independent of any sandbox.
It realizes the north star's bet #3 (filesystem interface, code-mode interaction) and bet #4 (portable, multi-sandbox) without weakening bet #2 (trust).

## User Stories

### Story: anchored-edit-anywhere

As a builder, I want hash-anchored, conflict-checked editing available as a standalone tool
my agent can call directly — not only inside a sandbox — so that any agent, sandboxed or
not, can make safe, reviewable edits to VFS files.

### Story: monty-code-mode

As a builder embedding an agent, I want the agent to run arbitrary Python over its VFS
through a natively mounted filesystem — with indexed `grep`/`find`/`glob` and anchored
`edit` available in-language — so that multi-step work composes inside one sandboxed program
instead of many tool round-trips.

### Story: just-bash-shell-tool

As a builder, I want to offer the agent a familiar Bash tool over its VFS — with index-backed
`grep`/`find`/`glob` — for exploration and shell-style composition, with safe authoring
provided by the standalone anchored-edit tool surfaced alongside it, so that the agent gets
ergonomic shell access without losing safe, conflict-checked edits.

### Story: governed-mount

As an operator, I want the mounted filesystem to enforce the same permissions, audit trail,
and optimistic-concurrency guarantees as direct VFS calls, so that native filesystem
ergonomics never become a path around governance.

### Story: portable-sandboxes

As a builder, I want Monty and just-bash to run over the same VFS through a single internal
filesystem contract, so that I am not locked to one execution engine and can add others
without touching the substrate.

## Scope

Two capabilities, foundation first: **anchored-editing** (the editing primitive both
sandboxes and bare agents consume), then **execution** (the mount and providers that build
on it).

**In scope — anchored-editing (promoted from `execution`):**

- Promote hash-anchored editing to its own capability, decoupled from the sandbox lifecycle.
- **Stateless indexed anchors**: an anchor is the line's **absolute index plus a short content-bound checksum** (`{index}:{checksum}`) — the index is the collision-free locator, the checksum an integrity/fabrication guard.
  No server-side state — no token pool, no stored map, no lifetime to manage, no difflib reconciliation.
- A standalone surface — `read_anchored(path, offset=None, limit=None)` returning content (full or
  a window), the file's version, and per-line anchors (absolute indices), and
  `edit_anchored(path, hunks, expected_version)` accepting one or more hunks — bound to a
  `(namespace, principal)` context and usable across separate calls without a sandbox.
- **Strict conflict policy:** an edit conflicts if the file changed at all since the anchors were read (carried via `expected_version`), consistent with the VFS `write` model.
  The index locates the edit; the version check and the checksum guard it.

**In scope — execution:**

- An internal **FS-port** contract: an async, whole-file, path-based filesystem interface
  backed by `Session` that enforces permission checks, audit, and CAS — the boundary between
  the VFS layer and the execution-environment layer.
- **Monty native mount**: an `AbstractOS` adapter routing the sandbox's intercepted `open`/`pathlib`/`os` path operations to the FS-port, with a sync→async bridge (Monty dispatches FS callbacks off the host loop — verified by spike).
  The existing injected verbs are **kept additively** — native I/O is added alongside `cat`/`ls`/`grep`/`find`/`edit`.
- **just-bash provider**: injects an FS-port-backed `IFileSystem` (`fs=`) and overrides
  `grep`/`find`/`glob` (via `commands=`) to route to `session.search` for index-backed
  acceleration (parity with Monty).
- **Dependency layering**: `monty`/`just-bash` as granular optional extras plus a `codemode`
  umbrella; the VFS layer installs standalone (pyproject done; lockfile + CI gating follow-on).
- **Mount-model documentation**: specs and adapter docstrings state that the mount is an
  interpreter-level virtual-filesystem **proxy into the governed VFS**, not an OS/FUSE mount;
  the host filesystem is never exposed to the sandbox.
- **Reconcile stale references**: update the execution spec's `AnchorMap`/extra-gating
  requirements and the stale Monty API prose in the research docs.
- A **pydantic-ai sample consumer** and a disposable **LangGraph gut-check sketch**
  (non-normative, outside the library) that drive `vfs.execute` and the anchored-edit tool
  over a mounted sandbox using only the public surface — evidence the surface is
  framework-agnostic.

**Out of scope:**

- Converting the VFS core to synchronous — it stays async; the bridge is the only sync surface.
- **Permissive / region-level edit merge** — an edit conflicts on any concurrent change to the file, not only to its anchored region.
  Region-level concurrent merge is a future opt-in with its own story, not carried speculatively here.
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

- **Stateless indexed anchors.**
  An anchor is `{absolute_line_index}:{checksum}` (e.g. `47:9c2`, `checksum = blake3(index⊕line)[:3]`).
  The literal index is the collision-free locator (identical boilerplate lines are uniquely targetable); the checksum is an integrity guard catching index transposition, cross-file paste, and hallucinated indices.
  `edit_anchored` conflicts if the file changed since the anchors were read (strict).
  Statelessness lets the standalone tool work across independent calls without a stored map.
  This replaces the per-`execute` `AnchorMap`.
- **FS-port = weakest common denominator** of Monty's `AbstractOS` needs, just-bash's `IFileSystem`, and our `Session` ops: `read`/`write`/`list`/`stat`/`exists`/`delete` (plus `mkdir` as a prefix no-op).
  Every method calls `Session`, so permission/audit/CAS hold at the port; the host OS is never touched.
  POSIX extras just-bash declares (`symlink`/`chmod`/`utimes`) sit above the floor and raise unsupported.
- **Monty bridge**: Monty dispatches `os=` callbacks on a worker thread (spike: `same_thread: False`), so each sync callback drives the async FS-port via `asyncio.run_coroutine_threadsafe(coro, host_loop).result()`.
  No core change; one bounded concern — each in-flight FS call parks a worker thread (note for heavy fan-out).
- **just-bash adapter**: `IFileSystem` is fully async, so the adapter is a direct passthrough to
  the FS-port — no bridge (spike-verified). `grep`/`find`/`glob` are replaced via `commands=`
  with versions that call `session.search` (spike-verified).
- **Native-mount writes are last-writer-wins**: native `open(...).write()` carries no version stamp, so mount writes use the VFS default (last-writer-wins with bounded retry).
  Compare-and-swap editing is reached through `edit_anchored`'s `expected_version`.
  Permissions and audit hold on every mount op regardless.
- **Spikes**: Monty threading model (done — bridge verified); just-bash `fs=` passthrough +
  `commands=` override (done — verified; one `commands=`/`cat` interaction to pin before
  implementing, see `design.md`).

## Open Questions

- **Checksum length** — 2 vs 3 hex chars; affects fabrication-guard strength only, not targeting.
  Settled empirically during implementation (see `design.md`).
- **Consumer gut-check depth** — how thin the LangGraph sketch should be; it exists only to
  falsify coupling, not to ship a LangGraph integration.
