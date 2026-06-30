# Proposal: Anchored Editing (capability + freshness model) — UNRESOLVED

> Status: **exploration / decision space**, not yet ready to implement. Split out of
> `2026-06-28-sandbox-fs-mount`, which was narrowed to "code-mode tools over the VFS" and
> deliberately dropped anchored reads/edits. This document preserves the design dialogue so the
> capability can be resolved as its own change. Do not implement until the open questions below
> are settled.

## Intent

Give an agent **token-efficient, conflict-safe editing** of VFS files: read lines, then edit by naming a line span + replacement text without re-emitting unchanged content, with a guarantee that an edit never silently overwrites a change that landed since the read.
The capability should be usable both inside a code-mode sandbox (Monty, just-bash, others) and as a standalone tool any agent framework can call.

Prior art: Anthropic "code execution with MCP", Dirac hash-anchors, and shipped `oh-my-pi/hashline`
(line-number locator + file-level integrity tag, "re-ground after every edit").

## The core problem

Two concerns are easy to conflate and must stay separate:

1. **Locate + prove the line** — which line is being edited, and that the agent actually read it (anti-fabrication, anti-transposition, proof-of-read).
   A content-derived `index:checksum` anchor handles this and is purely line-local.
2. **Freshness** — "did the file change between the read and the edit?"
   This is a property of the read→edit **transaction**, not of any line.
   It must catch a concurrent change the agent never saw — including a change to an _interior_ line of a replaced span (see below).

### Why freshness needs a whole-file witness (the load-bearing example)

The edit primitive replaces an inclusive span (e.g. lines 5–10), so the agent's decision rests on the **whole span** — but to stay token-efficient it transmits only the **endpoints** (5 and 10) plus the replacement.
The interior (6–9) is never re-sent, so it is never verified.
A concurrent **in-place edit to line 8** (indices unshifted; lines 5 and 10 byte-identical) passes any endpoint-only check; splicing the span then silently destroys that edit.
Detecting it requires a witness covering the whole file (or at least the whole span), not just the endpoints.

A natural witness is a **content hash (ETag)** of the file's bytes — "are these the exact bytes I read?"
— which a change-then-revert-to-identical does not falsely trip, and which is content-local rather than an abstract version ordinal. (`VersionMeta` already carries `content_hash`, `version_id` (ULID, never reused), and `version_number` (per-path ordinal); `version_number` is the weakest witness because it can reset on delete-recreate.)

## The crux: where does the freshness witness live?

The line anchor only needs to locate/prove the line; the freshness witness can live elsewhere.
The hard requirement under debate is **the agent must never see, track, or pass the witness** — it is store bookkeeping with no meaning to the agent's task.
Options considered (the option space):

1. **Explicit caller version** — `edit_anchored(path, hunks, expected_version)`.
   Clean CAS, deterministic, easy to test; bad agent ergonomics (agent carries bookkeeping).
   _This was the original implementation shape._
2. **Freshness folded into each line anchor** — `anchor = index:hash(snapshot, index, line)`, `edit_anchored(path, hunks)` with no version arg.
   Great ergonomics, stateless across calls, portable; but every line token carries file-snapshot freshness even though freshness is a transaction property, and it collapses the "stale vs. fabricated" diagnostic.
3. **Visible snapshot / edit token** — read returns anchors + an `edit_token`; edit takes the token.
   Cleaner than baking into every line; line anchors stay line-local; but the agent carries an extra thing unless a wrapper hides it.
4. **Hidden tool metadata** — read returns visible anchored text; the tool framework stores a hidden `read_context_id`; edit receives it automatically (ETag-hidden-by-middleware / unit-of-work identity map).
   Best contract _if_ the tool layer supports hidden per-call state; lock-in to such a layer; harder for "any arbitrary framework" without a portable wrapper contract.
5. **Server-side read lease keyed by (principal/session/path)** — agent carries nothing; anchors
   stay simple; but ambiguous fast (multiple reads, windowed reads, concurrent agents, retries).
6. **Server-side read lease with explicit context id** — precise, supports windows/multi-read/
   multi-agent; costs lifecycle (expiry, cleanup, storage, "context missing" behavior).
7. **Span / read-set hash** — include a hash of the replaced span; targets the real read-set and
   could allow disjoint concurrent edits; opens permissive-merge semantics; more complex, less
   aligned with strict conflict.
8. **Full snapshot handle** — read returns a snapshot handle; edits target it and CAS against current.
   Clean domain model; likely too much ceremony for the agent-facing API.

### The trilemma

Any two are easy; all three force stateful harness infrastructure:

1. freshness witness **distinct** from the line anchor,
2. witness **invisible** to the agent,
3. **precise** under arbitrary multi-read / multi-agent / async / distributed flows.

On edit the agent hands over only `path + anchors + replacement`; recovering "_which read's_
witness" from that is impossible unless the witness is in the anchor (rejected by (1)), carried as a
visible/framework-hidden read handle (relaxes (2) or needs framework support), or approximated by
"latest read of path" (relaxes (3) — the unsafe leg-4 window below).

### How it scales (the harness-level question)

With a harness stash of `read → ETag`, supplied to `edit` as `if_match`:

- **Single user / agent / sequential / sync:** stash keyed by `path`; trivial and correct.
- **Multiple agents / users:** key by `(principal, path)`; the VFS `if_match` CAS is the real safety
  net — a wrong/missing witness yields a _conflict_, never a bad write.
- **Concurrent / async within one agent:** CAS serializes; first wins, others conflict (liveness cost).
- **Multiple / windowed reads of one path:** the genuinely unsafe leg — "latest ETag for path" can validate a span edit made from a _stale_ read (endpoints unchanged, interior changed) → silent loss.
  Safety here requires binding the edit to the _specific_ read (a read-context id).
- **Distributed / resumed / serverless harness:** an in-memory stash doesn't survive a process
  boundary; needs durable context storage, a request-carried token, or (degenerate) the folded-in
  witness of Option 2.

## Likely contract split (to validate)

- **VFS side (stable):** `edit_anchored(path, hunks, if_match=<content-hash>)`, CAS on the hash; the
  anchor stays a pure line locator (`index:checksum(index, line)`); `read_anchored` returns content +
  anchors + the ETag and **mints from a single consistent snapshot** (resolve version once, read that
  immutable version's bytes, never stat-then-read-latest).
- **Harness side (variable):** owns read→edit ETag continuity, at a sophistication it picks — path-keyed minimal (single-agent-sequential) up to a durable read-context store (multi-read / distributed).
  The CAS is the backstop; harness imperfection costs conflicts, not corruption — except the leg-4 stale-read span case, which needs read-context binding to fully close.

## Open questions to resolve before implementing

- Is leg-4 (multi/windowed reads with a stale-read span edit) in scope to make safe?
  If yes → read-context store + a portable hidden-context contract; if "edit from your latest read of a path" is an acceptable documented rule → path-keyed minimal harness suffices.
- For "any arbitrary framework," is a **visible-but-opaque** read handle (the agent passes it back blindly, never interprets it) acceptable?
  It makes legs 4 and 5 correct without per-framework hidden state, at the cost of one opaque param.
  Where is the line: "no _semantic_ state the agent must understand" vs. "no _extra params_ at all"?
- Witness type: `content_hash` (byte-state freshness, no spurious revert conflict) vs. `version_id` (names a snapshot, stricter).
  Write CAS still pivots on the version regardless.
- Edit primitive: keep inclusive-span replacement (then pure insert = one-line adjacent replace,
  repeat the line + new line(s)), or define endpoint-only insert/delete ops so the read-set equals the
  verify-set and no whole-file witness is needed at all?

## Disposed prior surface (what the excision removed)

The `2026-06-28-sandbox-fs-mount` change carried a (twice-redesigned) anchored-editing implementation that was fully removed when this capability was split out.
A restart should compare its scope against this inventory rather than re-deriving it:

- **Module** `src/vfs/anchored_editing/` — `anchors.py` (`checksum`, `make_anchor`,
  `anchors_for_lines`, `parse_anchor`, `resolve_anchor`, `K_DEFAULT`; anchor =
  `index:blake3(index ⊕ line)[:3]`) and `editor.py` (`AnchoredEditor.read_anchored` /
  `edit_anchored`, `AnchoredReadResult`, `AnchoredEditResult`, `Hunk`).
- **Public surface** (`vfs.__init__`): `AnchoredEditor`, `AnchoredReadResult`,
  `AnchoredEditResult`, `Hunk`, `make_anchor`.
- **Errors** (`vfs.errors`): `AnchorConflictError`, `ContentDecodeError`, and `vfs.execute`'s
  `anchor_conflict` translation row.
- **Shell-ops surface** (`vfs.execution.fs_ops`): the `edit` verb
  (`edit(path, start_anchor, end_anchor, replacement, expected_version=None)`); the `anchors`
  the read verbs returned — `cat`/`head`/`tail` previously returned
  `{"lines", "anchors", "error"}` and now return `{"lines", "error"}`; the `anchor_map`
  parameter of `fs_operations_for`; `write`'s `anchor_map.invalidate`.
- **Monty provider**: `edit` in the injected `external_functions` (the shell-verb set).
- **Baseline spec** (`specs/execution`): the `AnchoredEditing` requirement — a session-scoped
  `AnchorMap` with a single-token pool, `(path, version_number, line_index, line_content)`
  entries, `validate`, difflib (`SequenceMatcher`) `reconcile`, and invalidate-on-raw-write.

Two distinct designs were disposed: (1) the original **stateful** `AnchorMap` (token pool + difflib reconcile), and (2) a **stateless** redesign (content-derived `index:checksum`, caller-supplied `expected_version`, later folded-version variant).
Neither resolved the freshness-witness question above; the restart starts from the option space, not from either.

## Non-goals (for whoever picks this up)

- Do not couple the VFS layer to a specific tool/session framework; if a read-context store is
  chosen, define a portable wrapper contract, not a hard dependency.
- Do not adopt permissive region-merge without its own story; strict conflict-and-retry is the floor.
