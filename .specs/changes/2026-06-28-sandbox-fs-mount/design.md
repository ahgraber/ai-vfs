# Design: Sandbox Filesystem Mount + Standalone Anchored Editing

## Context

- ai-vfs is **async throughout** (`Session`/`VFS` ops are coroutines).
  Nothing in this change converts the core to sync; the only sync surface is the Monty mount bridge.
- Two sandbox surfaces are now available in the installed deps and drive the design:
  - **pydantic-monty 0.0.18** exposes a native filesystem mount via `Monty.run_async(os=...)`, where `os` is an `AbstractOS` whose `__call__(function_name, args, kwargs)` intercepts the sandbox's `open`/`pathlib`/`os` operations.
    Its file callbacks are **synchronous**.
  - **just-bash 0.2.1** exposes `Bash(fs=IFileSystem, commands={name: Command})`; `IFileSystem`
    is **fully async**, and `Command` is a Protocol with async
    `execute(args: list[str], ctx: CommandContext) -> ExecResult`.
- Hash-anchored editing is promoted out of `execution` into its own `anchored-editing` capability and redesigned to be stateless, so any agent can call it as a tool without a sandbox.
  The two changes share one boundary contract — the **FS-port**.

## Decisions

### Decision: IndexedAnchors

**Chosen:** An anchor is the line's **absolute (file-relative) index plus a short content-bound checksum**, rendered `{index}:{checksum}` (e.g. `47:9c2`).
The **index carries identity** (the locator); the **checksum is an integrity/fabrication + proof-of-read guard** (not a locator).
No anchor-to-location map is persisted — anchors are reproducible purely from content.

**Concrete formula (mechanism, tunable):**

```text
anchor   = f"{abs_line_index}:{ck}"
ck       = blake3(f"{abs_line_index}\n{line_text}").hexdigest()[:3]   # blake3 already a dep; k=3 default
```

**Rationale:** Under `StrictEditConflict`, `version == expected_version` guarantees byte-identical content, so the **literal index is a perfect, collision-free locator** — it targets the exact intended line with probability 1, by construction, no fingerprint needed for uniqueness.
This is why the earlier pure-content-fingerprint + neighbor-window model was over-built: a neighbor window only earns its keep when anchors must _relocate across a change_ (Dirac's stateful/permissive world), which strict conflict forbids.
Dropping it removes code, removes the "identical-boilerplate block is un-editable" usability cliff (such a block is now trivially addressable by index), and keeps statelessness.

**Why keep the checksum, and why bind it to the index.**
The version check guards the _concurrency_ axis but structurally cannot guard the _agent-self-consistency_ axis.
The checksum is the only thing that extends "fail closed" (the `AnchoredEditConflicts` "no guessed location" contract) to: an anchor pasted from a **different file** at that file's current version; an **index transposition** (means 47, types 48); and an **in-range hallucinated** index — all of which would otherwise silently edit the wrong line.
Hashing **(index ⊕ content)** rather than content-only is what catches transposition _between two identical lines_ (content-only would let `47:9c2`→`48:9c2` pass; index-bound makes them differ) and authenticates the pair (alter the visible index without recomputing → mismatch → conflict).

**Length / collision (mechanism, tunable):** truncating a hash reintroduces birthday collisions, but here collisions are **harmless to targeting** — the literal index already pins the line; the checksum is a guard, so a `16^-k` clash only marginally weakens fabrication detection.
**Default `k=3` (1/4096).**
`k=2` (1/256) would suffice if the checksum were _only_ a version-CAS backstop, but it doubles as the **proof-of-read** guard (an agent editing a line it never displayed must guess the per-line tag), so the extra hex char is worth it; bump higher only if proof-of-read is weighted heavily.
The index is the only uniqueness-bearing component and is **never hashed-in as the sole identifier**.

**Absolute index (must-fix):** the index is file-absolute, identical for full and windowed (`offset`/`limit`) reads; the checksum hashes the absolute index, so anchors are window-independent.
A window-relative index would collide across reads.

**Prior-art alignment (canonical references).**
The authoritative sources are Can Bölük's "The Harness Problem" (blog.can.ac, 2026-02-12) and the shipped `@oh-my-pi/hashline` implementation (`can1357/oh-my-pi`, `packages/hashline`); Dirac builds on these.
Note the blog's _illustration_ uses per-line content hashes (`11:a3|code`) and warns line numbers are fragile — but the **shipped hashline tool abandoned that and uses line numbers** as the locator with a single file-level content hash (`[path#TAG]`) as a snapshot-scoped integrity guard.
That is the same pair of decisions we made (index = locator; content hash = integrity guard), so we align with where the canonical implementation actually landed — not with the blog illustration Dirac critiqued.
Our line-number-invalidation posture (any change → conflict → re-read) matches shipped hashline's "re-ground after every edit."

Where we deliberately differ, each for our context: (a) **version-CAS instead of hashline's `SnapshotStore`** — the file version _is_ the snapshot identity, giving the same guarantee statelessly; (b) **per-line checksum instead of one file-level hash** — costs ~3 chars/line but restores **proof-of-read**, which shipped hashline lost when it moved the hash to the file header; (c) **hard-reject instead of hashline's 3-way auto-merge** — safer at the concurrency boundary (correctness/safety-at-boundaries over convenience).

**Single-token note:** Dirac's single-token _guarantee_ is OpenAI-`o200k`-specific and relies on statefulness; for a model-agnostic library it is not worth chasing.
Per-line token cost is dominated by the index digits (paid by every scheme); the `:{ck}` adds ~4 chars — cheap insurance.
Keep the `:` separator as a clean token boundary the LLM must echo exactly.

**Prior-art features deliberately omitted (scope decisions, not oversights):** hashline's **tree-sitter block ops** (`SWAP.BLK`/`DEL.BLK`) — a real ergonomic/token win but language-specific; deferred for a general VFS. hashline's **stateful never-displayed-line rejection** — we get a _probabilistic_ equivalent free (an out-of-window line's anchor must guess the per-line checksum, `1/16^k`) and deliberately don't add the stateful display-tracking it requires.

**Region bounding:** the edited region is the inclusive span `start_anchor`..
`end_anchor` in the read content.
Because any change since the read is a conflict, the edit only ever applies against the exact content the anchors came from — no concurrent-insert / grown-span case exists.
`end_anchor` before `start_anchor` is a conflict.

**Edit shape:** `edit_anchored` takes **one or more hunks**, applied atomically against one `expected_version`; the **agent-facing result is success/failure (+ new version)** — never the file content or anchors (matches standard edit tools; avoids re-emitting the document).
`read_anchored(path, offset, limit)` is the only full-content surface and is rangeable, so a re-read after an edit fetches only the region of interest.
No Myers/difflib reconcile (its only payoff — anchor stability across edits — is moot under strict conflict; an optional future optimization is returning only changed-line anchors as a delta when responses are large).

### Decision: StrictEditConflict

**Chosen:** An anchored edit conflicts if the file changed **at all** since the anchors were read — `edit_anchored` carries `expected_version` (the version `read_anchored` returned) and conflicts when the current version differs.
There is no region-level permissive merge.

**Rationale:** This is the same model as the VFS `write` + `expected_version` path and the pre-existing anchored-edit behavior, so it adds no new concurrency mental model.
It eliminates the silent-data-loss hazard a region-level merge creates (an edit reconstructs the whole file from a read snapshot; relocating into _current_ content and committing without a full-file version check could overwrite or delete an unrelated concurrent change between the anchors).
Under strict conflict, a concurrent change → conflict → the agent re-reads (cheaply, a range) to get fresh anchors → retries.
Multi-agent shared-doc editing and a single agent's parallel edits both converge through conflict-retry; neither _requires_ permissive merge.

**Alternatives considered:**

- **Region-level permissive merge** (conflict only when the anchored region's text changed): enables concurrent disjoint edits without a conflict, but (a) risks silent loss of a concurrent insert _between_ the anchors, (b) is textual-not-semantic so disjoint edits can still break meaning, and (c) is motivated by no current user story.
  Deferred as a future opt-in with its own story, not carried speculatively (YAGNI).

### Decision: ConsistencyRestsOnCasFloor

**Chosen:** Anchored-edit correctness depends only on the metadata store's single-record
conditional-write (CAS) guarantee — the contract floor present on every backend — not on
read-your-writes freshness.

**Rationale:** The CAS write is evaluated atomically against the authoritative current version (e.g. Mongo `find_one_and_update` on the primary), so a stale read can only produce a conflict (safe retry), never a misapplied edit.
Eventual-consistency reads degrade liveness (more conflicts/retries), not safety.
Keeps `anchored-editing` backend-substitutable (LSP).

### Decision: FsPortAsWeakestCommonDenominator

**Chosen:** One internal async FS-port — `read`/`write`/`list`/`stat`/`exists`/`delete` + `mkdir`-noop, every method routed through `Session` — sits at the boundary between the VFS layer and the execution-environment layer.
Monty's `AbstractOS` and just-bash's `IFileSystem` are **adapters** onto it.
POSIX operations above the floor (`symlink`/`chmod`/`utimes`) raise unsupported.

**Rationale:** Same contract-floor / LSP discipline as `MetadataStore`/`BlobStore`: define the intersection of what every sandbox needs and what the VFS can govern, expose nothing only one side supports.
Governance (permissions, audit) lives in `Session` beneath the port, so no adapter can bypass it.

**Alternatives considered:**

- **fsspec as the bridge:** rejected — at the sandbox boundary fsspec only subtracts (loses
  anchored edit, CAS, op-budget, permission pruning, search routing) and adds a streaming half
  neither sandbox can use. fsspec's legitimate altitude is _below_ the blob store, out of scope
  here.
- **Per-sandbox bespoke filesystem code:** rejected — duplicates governance wiring per sandbox
  and has no shared substitutability contract.

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

### Decision: KeepInjectedVerbsAdditive

**Chosen:** Add the native mount **alongside** the existing injected verbs on Monty; do not
shrink the verb set. `cat`/`head`/`tail` keep returning content + anchors (now content-derived);
`edit` delegates to `anchored-editing`.

**Rationale:** The verbs carry agent affordances the native mount cannot — anchors (for anchored editing), search-index-routed `grep`/`find`/`glob`, structured `ls` metadata.
They are not redundant with `open`/`pathlib`.
Additive is minimal-scope and breaks no existing in-Monty workflow.
A later consolidation is a separate decision.

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
   │                         │
   │ (Session)               │ (Session)
   ▼                         ▼
anchored-editing         FS-port  (async; read/write/list/stat/exists/delete + mkdir-noop)
 read_anchored              ▲  ── boundary #2 (this change)
 edit_anchored              │
   │                ┌───────┴────────────────────────┐
   │                │                                │
   │          Monty AbstractOS adapter        just-bash IFileSystem adapter
   │          (sync callback →                (async passthrough)
   │           run_coroutine_threadsafe)      + grep/find/glob via commands= → session.search
   │                │                                │
   ├── injected `edit` verb (Monty)           companion edit tool (agent framework)
   │
   └── standalone tool  (any agent framework: pydantic-ai sample, LangGraph sketch)
```

- The mount is an **interpreter-level virtual filesystem** — a proxy into the governed VFS.
  No FUSE, no OS mount, no host filesystem exposure.
- Native-mount writes are last-writer-wins (no version stamp on `open(...).write()`); CAS is reached through `edit_anchored`'s `expected_version`.
  Permissions + audit hold on every op.

## Risks

- **Worker-thread parking (Monty bridge):** each in-flight native FS call blocks one Monty worker thread until the host-loop coroutine completes.
  Fine for serial-IO sandboxes; under heavy parallel FS fan-out it bounds throughput.
  Mitigation: document; revisit a bounded worker pool only if profiling shows contention.
- **just-bash maturity:** 0.2.1 is pre-release and a third-party port.
  Mitigation: pin the version, own a conformance test over the `fs=` adapter and the overridden commands, and treat the provider as optional (extra).
- **just-bash `commands=` semantics unverified at edges:** the spike showed override works but also a `cat`-returns-empty anomaly after `commands=` and that `CommandContext` exposes neither `fs` nor `cwd`.
  Mitigation: at implementation, confirm merge-vs-replace of the registry and how the overridden command resolves the working directory / scope (likely threaded via the session adapter, not `ctx`).
- **Checksum collisions weaken the fabrication guard (not targeting):** a short checksum can clash (`16^-k`), letting a wrong-but-in-range index slip past the guard.
  Targeting is unaffected — the literal index already pins the line.
  Mitigation: `k=3` default (1/4096) given the checksum's dual role as proof-of-read; raise further only if proof-of-read is weighted heavily.
- **Cross-store revive race:** pre-existing and accepted at PoC scale (storage spec); orthogonal
  to this change.

## Dropped Scenarios (deliberate)

- **`WriteInvalidatesAnchors`** (baseline `execution` `ShellOperationsLayer`): removed because stateless anchors have nothing to invalidate — an anchor over changed content simply fails to resolve at edit time (`AnchoredEditConflicts`).
  The behavior it guarded is now covered by the `anchored-editing` conflict scenarios.
  Marked in the delta with `<!-- modified-removes: WriteInvalidatesAnchors -->`.

## Review Dispositions

Findings from the fresh-eyes review that were **declined**, recorded for the audit trail:

- **"`ConsistencyFloor` overclaims that every backend provides conditional-write."**
  Declined — it does not overclaim.
  Storage `MetadataCASSemantics` explicitly makes single-document CAS a mandatory contract floor "every adapter in both families provides."
  The cross-store revive race the reviewer cited is about blob GC, not metadata CAS.
  The requirement now cross-references `MetadataCASSemantics`.
- **"`FsPortContract` over-specifies an internal interface by listing its methods."**
  Declined — the FS-port is a protocol boundary that future sandbox providers depend on, exactly like `MetadataStore`/`BlobStore`, which the baseline storage spec enumerates _with their methods as contract_.
  Enumerating the FS-port methods is consistent with that precedent, not a §1.4 violation.

Accepted findings (permissive-edit cut, dangling `AnchorMap` references, drop marker, mount error-preservation, single-flight note, anchor edge-case partitions, error-model convention, just-bash spike elevation, proposal headings, tombstone behavior) are folded into the revised artifacts.

## Open Questions (non-blocking)

- Checksum length `k` — `k=3` default; tune empirically.
  Affects fabrication/proof-of-read guard strength only, not targeting.
- LangGraph gut-check depth — keep it a thin "can it mount and edit?"
  sketch; it exists to falsify coupling, not to ship a LangGraph integration.
