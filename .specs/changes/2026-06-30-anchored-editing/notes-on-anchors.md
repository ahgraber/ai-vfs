The key split is: **the VFS primitive should be explicit; the harness adapter can be implicit.**

If the core `edit_anchored` silently depends on "whatever this agent last read," that locks hidden session state into the storage/editing contract.
Reversal gets expensive once multiple agents, multiple reads, retries, or non-Monty harnesses enter.
So I'd keep hidden state out of the core and put it in harness adapters.

**Layer 1: Core Anchored Editing** The underlying library should expose an ETag-style conditional edit:

```python
read_anchored(path, offset=None, limit=None) -> AnchoredReadResult(
    lines=...,
    anchors=...,        # short line-local anchors
    validator=...,      # full content_hash or version_id
)

edit_anchored(path, hunks, if_match=validator) -> AnchoredEditResult
```

Core behavior:

1. `read_anchored` resolves one current snapshot and reads exactly that snapshot's bytes.
2. It returns line anchors plus a full snapshot validator.
3. `edit_anchored` compares `if_match` to the current snapshot validator.
4. If mismatch, conflict.
5. If match, validate line anchors against current bytes.
6. Write with CAS against the current `version_number`.

That is the deterministic safety boundary.
The agent does not need to see this API directly.

**Layer 2: Harness Read Context** Harnesses can hide the validator in a read-context store:

```python
ReadContext(
    id,
    namespace,
    principal,
    path,
    validator,
    anchors_returned,
    created_at,
)
```

The harness-visible read tool returns only anchored text.
The harness stores the validator.
The harness-visible edit tool accepts only path + anchors + replacement, then injects `if_match` when calling core `edit_anchored`.

For sequential single-agent work, "latest read context for this path" is enough.
Once any assumption changes, latest-by-path is too weak.

**If Assumptions Change** Multiple reads of same path: Use a read context id, hidden metadata, or match the edit anchors to the context that emitted them.
If multiple contexts match and validators differ, reject as ambiguous and ask for a re-read.

Multiple agents: Scope contexts by `(namespace, principal, harness_run_id, path)` at minimum.
Never use a global latest read for a path.

Multiple users/principals: The context does not grant permission.
Re-authorize on edit.
The stored validator only says "what snapshot was read," not "this caller may write."

Parallel edits: Both edits may carry the same validator and pass the first freshness check.
The write CAS still ensures only one commits; the loser gets conflict.

Cross-turn or cross-process harnesses: Either persist read contexts with expiry, or expose a visible edit token/ETag.
Hidden in-memory state is not enough.

**Monty** For Monty, do we need to re-determine whether we make native `open()` participate in anchored editing?
Native reads/writes remain ordinary filesystem I/O.
And then we can determine whether we want to include the anchored functions injected or not; i'm not sure they make sense in the monty flow (because they would require two monty sessions a read and an edit so the agent can see the anchors...)

Provide separate injected anchored affordances:

```python
read_anchored(path, offset=None, limit=None) -> str
edit_anchored(path, start_anchor, end_anchor, replacement) -> result
```

Internally, `read_anchored` stores a read context in the provider/operation layer. `edit_anchored` finds the matching context and calls core:

```python
core.edit_anchored(path, hunks, if_match=context.validator)
```

For Monty, because callbacks/functions are inside one execution run, an in-memory context store is acceptable.
If a sandbox can perform overlapping guest tasks later, make the store concurrency-safe and context matching explicit.

**Direct Harnesses** For PydanticAI/LangGraph/etc., offer two surfaces:

1. Low-level library API:
   Returns `validator`; caller owns it.

2. Agent tool helper:
   Hides validator in harness state.

```python
tool.read_anchored(path)  # visible anchored text, hidden context stored
tool.edit_anchored(path, hunks)  # injects if_match internally
```

If a harness cannot preserve hidden state, it must expose a visible `edit_token` or `etag` or whatever object name we align on.
There is nowhere else for deterministic freshness to live.

perhaps:

```text
Core API: explicit if_match.
Monty adapter: hidden per-run read context.
Agent harness helpers: hidden read context when possible, visible edit token fallback.
Line anchors: short, line-local, not freshness-bearing.
Freshness: full validator, never truncated.
```
