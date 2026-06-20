# North Star — ai-vfs

> The product's reason for being. Durable, project-global singleton; changes rarely.
> Apex of the value chain **north star → user story → requirement**:
> every change's user stories (`proposal.md`) ladder up to this,
> and each delta requirement backlinks to a story via `Serves:`.
> A change whose stories don't connect here is drifting from product intent — surface that rather than proceeding.

## Elevator pitch

ai-vfs gives autonomous agents a **durable place to work**: a versioned, permissioned
virtual filesystem where an agent can read, write, search, and run code over files it
owns — with the safety scaffolding that makes handing write access to a non-deterministic
actor a defensible decision.

It is a **library-first SDK**: one workspace abstraction that runs on a laptop
(SQLite + local files) for development and, unchanged, on a production stack — a
relational or document database for metadata, S3-compatible object storage for blobs.

## Who it's for

The **builder or operator embedding agents into a product** — someone who must give one
or many agents a persistent, governed workspace over shared data, and be able to answer
afterward: _what did the agent do, and can I undo it?_

## The job to be done

Let an agent **do work that survives the session** — draft and revise artifacts, build and reorganize a knowledge base, run multi-step transforms — and keep that work safe, attributable, and isolated.
A read-only retrieval lens over an existing corpus cannot hold an agent's output; ai-vfs is where the output lives.

## Defining bets (what keeps the product coherent)

1. **Authorship, not retrieval.**
   Files are the agent's durable source of truth, not a read-only view of a corpus stored elsewhere.
2. **Trust is a feature, not an add-on.**
   Because the writer is a non-deterministic agent, every change is reversible (versions + rollback), attributable (audit), and contained (namespaces + path permissions with invisible pruning).
   These are what make delegating write access acceptable in the first place.
3. **Filesystem interface, code-mode interaction.**
   Agents work through familiar shell verbs and by writing code that composes operations — not through a sprawl of bespoke tools.
4. **Portable, embeddable, no lock-in.**
   One contract from local dev to enterprise backends; dropped into any Python agent framework as a library.

## What it is deliberately not

- **Not a read-only retrieval / RAG lens** over an external corpus — it owns the files
  agents create.
- **Does not require a VM or container** — full virtual-filesystem functionality is available with in-process interpreters alone (the default profile).
  A heavier sandbox may exist only as an optional, non-required execution backend; how ai-vfs is itself deployed (e.g. inside a container) is out of scope.
- **Not a human real-time collaborative editor** — no CRDT / co-editing; the writer is an
  agent.
- **Not a general database or object store** — a filesystem abstraction with versioning
  and search, not a query engine.
