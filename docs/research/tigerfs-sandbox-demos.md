# TigerFS + sandbox demos: bashkit and Monty

Two proof-of-concept explorations of the same question — can an in-process sandbox be given a Postgres-backed filesystem via TigerFS?
— run against two different sandboxes.
Both land on the same shape of answer (a thin Python bridge translating the sandbox's file callbacks into POSIX I/O on the TigerFS mount), but differ in how directly bashkit and Monty can compose with it.

---

## Part 1: bashkit + TigerFS

_A conversation exploring whether everruns/bashkit can use TigerFS as its virtual filesystem, with a pydantic-ai demo and architectural analysis._

### Initial request

**User:** i want to give everruns/bashkit a filesystem using the new postgres-backed filesystem called tigerfs. can you build a demo showing whether it is possible?
use pydantic-ai as the agent framework and assume openrouter as the model api provider.

provide the minimal example that proves the demo. do not write overly defensive or hedging code.
KISS.

### Research findings

This demo wires together two tools whose full fact sheets live in [`vfs-landscape-and-fit.md`](vfs-landscape-and-fit.md) — **bashkit** (§1.2) and **TigerFS** (§1.1).
The two facts the demo turns on: bashkit's `FileSystem` trait lives in-process (a Rust `HashMap`), and TigerFS is a kernel-level FUSE/NFS daemon mounting a PostgreSQL database — different layers, which is the crux of the architectural finding below.

### Architectural finding

**Direct composition is not possible.**

bashkit's `FileSystem` trait lives entirely inside Rust process memory.
TigerFS operates at the kernel FUSE layer.
There is no API seam — you cannot pass a FUSE mount as a `FileSystem` impl to bashkit's builder.

```text
bashkit (Rust in-process)          TigerFS (OS-level)
┌─────────────────────────┐        ┌─────────────────────────┐
│ Bash interpreter        │        │ Unix VFS layer          │
│ InMemoryFs (HashMap)    │   ✗    │ FUSE daemon             │
│ FileSystem trait        │        │ PostgreSQL              │
│ Python BashTool         │        │ Shared across processes │
└─────────────────────────┘        └─────────────────────────┘
         different abstraction layers
```

**What does work:** a sync bridge. bashkit is the fast ephemeral scratch space for one agent turn; Postgres (via asyncpg, the same DB TigerFS uses) is the durable per-user store.
On save/restore, the VFS state is checkpointed as JSON rows.

### Use case clarification

**User:** "TigerFS is a FUSE/NFS OS-level mount" — I guess maybe we should explore this further.
My intended use case is to use tigerfs as a virtual filesystem for a chatgpt-style webapp. instead of allocating virtual machines to each user, my hope was to provide something similar to bashkit + tigerFS for a cloud-native proxy for a virtual machine, container, or local sandbox. the ultimate goal was something similar to the power and expressiveness of claude code, but running as a webapp with multiple concurrent users.

### Architecture for the webapp use case

For a multi-tenant Claude Code-style webapp:

- Each HTTP session gets a `Bash()` instance (cheap — no VM, no container per user)
- Each user has a row in `agent_fs_snapshots` in Postgres
- Many concurrent users, no per-user infrastructure
- Durable state between chat turns via Postgres snapshots
- Humans with a `tigerfs mount` on the same DB can see agent-written files as real rows (and vice versa after `restore_fs`)

The agent gets three tools:

| Tool         | What it does                                                                 |
| ------------ | ---------------------------------------------------------------------------- |
| `bash`       | bashkit's built-in tool — agent writes/runs code in the ephemeral VFS        |
| `save_fs`    | walks `find`, base64-encodes every file, writes a JSONB row to Postgres      |
| `restore_fs` | pulls most recent snapshot row, resets bashkit, replays files into fresh VFS |

**The real tradeoff:** bashkit cannot run arbitrary binaries or do network I/O — it's a bash interpreter, not a full container.
That's the gap from Claude Code to evaluate.

### Demo code

```python
"""
bashkit + TigerFS demo
======================
Question: can TigerFS back bashkit's virtual filesystem?

Finding: No direct plug-in. bashkit's FileSystem trait lives in Rust process
memory (a HashMap). TigerFS is a kernel-level FUSE/NFS daemon. Different layers.

What works: a sync bridge. bashkit is the fast ephemeral scratch space for one
agent turn; Postgres (via asyncpg, same DB TigerFS uses) is the durable
per-user store. On save/restore you checkpoint VFS state as JSON rows.

This demo shows a pydantic-ai agent with:
  - bash tool  (bashkit VFS)
  - save_fs    (snapshot VFS → Postgres)
  - restore_fs (Postgres → fresh VFS)

Run:
    OPENROUTER_API_KEY=sk-or-... POSTGRES_DSN=postgres://... python demo.py

Without real credentials it runs in dry-run mode so you can see the wiring.
"""

import asyncio
import json
import os
from typing import Any

import asyncpg
import bashkit
from bashkit.pydantic_ai import create_bash_tool
from pydantic_ai import Agent, RunContext, Tool
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider


# ── VFS snapshot helpers ──────────────────────────────────────────────────────


def vfs_snapshot(bash: bashkit.Bash) -> dict[str, str]:
    """Walk the bashkit VFS and return {path: content} for all user files."""
    result = bash.execute_sync("find /home /tmp -type f 2>/dev/null")
    snapshot: dict[str, str] = {}
    for path in result.stdout.splitlines():
        path = path.strip()
        if not path:
            continue
        r = bash.execute_sync(f"cat {path} 2>/dev/null | base64")
        snapshot[path] = r.stdout.strip()
    return snapshot


def vfs_restore(bash: bashkit.Bash, snapshot: dict[str, str]) -> None:
    """Replay a snapshot into a fresh bashkit VFS."""
    for path, b64_content in snapshot.items():
        bash.execute_sync(f"mkdir -p $(dirname {path}) && echo '{b64_content}' | base64 -d > {path}")


# ── Postgres persistence (same DB TigerFS uses) ───────────────────────────────


async def ensure_schema(conn: asyncpg.Connection) -> None:
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_fs_snapshots (
            user_id   TEXT        NOT NULL,
            saved_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            snapshot  JSONB       NOT NULL,
            PRIMARY KEY (user_id, saved_at)
        )
    """)


async def pg_save(conn: asyncpg.Connection, user_id: str, snapshot: dict) -> None:
    await conn.execute(
        "INSERT INTO agent_fs_snapshots (user_id, snapshot) VALUES ($1, $2)",
        user_id,
        json.dumps(snapshot),
    )


async def pg_load(conn: asyncpg.Connection, user_id: str) -> dict | None:
    row = await conn.fetchrow(
        "SELECT snapshot FROM agent_fs_snapshots WHERE user_id = $1 ORDER BY saved_at DESC LIMIT 1",
        user_id,
    )
    return json.loads(row["snapshot"]) if row else None


# ── Agent deps ────────────────────────────────────────────────────────────────


class Deps:
    def __init__(self, bash: bashkit.Bash, pg: asyncpg.Connection | None, user_id: str):
        self.bash = bash
        self.pg = pg
        self.user_id = user_id


# ── pydantic-ai tools ─────────────────────────────────────────────────────────


def make_save_tool() -> Tool:
    async def save_fs(ctx: RunContext[Deps]) -> str:
        """Persist the current virtual filesystem to Postgres."""
        snap = vfs_snapshot(ctx.deps.bash)
        if ctx.deps.pg:
            await pg_save(ctx.deps.pg, ctx.deps.user_id, snap)
            return f"Saved {len(snap)} files to Postgres for user {ctx.deps.user_id}."
        else:
            return f"[dry-run] Would save {len(snap)} files: {list(snap.keys())}"

    return Tool(save_fs, takes_ctx=True, name="save_fs")


def make_restore_tool() -> Tool:
    async def restore_fs(ctx: RunContext[Deps]) -> str:
        """Restore the virtual filesystem from the most recent Postgres snapshot."""
        if not ctx.deps.pg:
            return "[dry-run] No Postgres connection; nothing to restore."
        snap = await pg_load(ctx.deps.pg, ctx.deps.user_id)
        if not snap:
            return "No snapshot found for this user."
        ctx.deps.bash.reset()
        vfs_restore(ctx.deps.bash, snap)
        return f"Restored {len(snap)} files from Postgres."

    return Tool(restore_fs, takes_ctx=True, name="restore_fs")


# ── Build the agent ───────────────────────────────────────────────────────────


def build_agent(model_name: str = "anthropic/claude-3-haiku") -> Agent:
    api_key = os.environ.get("OPENROUTER_API_KEY", "dry-run")
    provider = OpenAIProvider(
        base_url="https://openrouter.ai/api/v1",
        api_key=api_key,
    )
    model = OpenAIChatModel(model_name, provider=provider)

    return Agent(
        model,
        deps_type=Deps,
        tools=[
            create_bash_tool(),  # bashkit's built-in pydantic-ai tool
            make_save_tool(),
            make_restore_tool(),
        ],
        system_prompt=(
            "You are a coding assistant with a sandboxed bash environment. "
            "Files you create persist only for this session unless you call save_fs. "
            "Use restore_fs at the start of a session to reload previous work."
        ),
    )


# ── Demo entry point ──────────────────────────────────────────────────────────


async def run_demo():
    dsn = os.environ.get("POSTGRES_DSN")
    user_id = "demo-user-1"

    pg: asyncpg.Connection | None = None
    if dsn:
        pg = await asyncpg.connect(dsn)
        await ensure_schema(pg)
        print(f"[postgres] connected to {dsn}")
    else:
        print(
            "[dry-run] No POSTGRES_DSN — running without real Postgres.\n"
            "          Set POSTGRES_DSN=postgres://user:pass@host/db to enable persistence.\n"
        )

    bash = bashkit.Bash(username="agent", hostname="sandbox")
    deps = Deps(bash=bash, pg=pg, user_id=user_id)
    agent = build_agent()

    # ── Turn 1: do some work and save ────────────────────────────────────────
    print("=== Turn 1: create files and save to Postgres ===")
    task1 = (
        "Create a file /home/agent/hello.py containing a Python hello-world script, "
        "then create /home/agent/notes.md with a one-line note, "
        "then call save_fs to persist the filesystem."
    )

    if os.environ.get("OPENROUTER_API_KEY"):
        result1 = await agent.run(task1, deps=deps)
        print("Agent:", result1.output)
    else:
        # Simulate without LLM to show wiring
        print("[dry-run] Simulating agent turn (no OPENROUTER_API_KEY)...")
        bash.execute_sync(
            "mkdir -p /home/agent && "
            "echo 'print(\"hello world\")' > /home/agent/hello.py && "
            "echo '# Notes' > /home/agent/notes.md"
        )
        snap = vfs_snapshot(bash)
        print(f"  VFS snapshot: {list(snap.keys())}")
        if pg:
            await pg_save(pg, user_id, snap)
            print(f"  Saved to Postgres (table: agent_fs_snapshots, user: {user_id})")
        else:
            print(f"  [dry-run] Would save to Postgres: {snap}")

    # ── Turn 2: fresh bash instance, restore from Postgres ───────────────────
    print("\n=== Turn 2: new bash session, restore from Postgres ===")
    fresh_bash = bashkit.Bash(username="agent", hostname="sandbox")
    fresh_deps = Deps(bash=fresh_bash, pg=pg, user_id=user_id)

    before = fresh_bash.execute_sync("find /home -type f 2>/dev/null")
    print(f"  Before restore — files in VFS: {repr(before.stdout.strip() or '(none)')}")

    if pg:
        snap = await pg_load(pg, user_id)
        if snap:
            vfs_restore(fresh_bash, snap)
            after = fresh_bash.execute_sync("find /home -type f 2>/dev/null")
            print(f"  After restore  — files in VFS: {after.stdout.strip()}")
            content = fresh_bash.execute_sync("cat /home/agent/hello.py")
            print(f"  hello.py contents: {content.stdout.strip()}")
    else:
        print("  [dry-run] Would restore snapshot from Postgres into fresh bashkit VFS.")

    if pg:
        await pg.close()


if __name__ == "__main__":
    asyncio.run(run_demo())
```

### Running it

```bash
# dry-run (no credentials needed)
python demo.py

# with real backends
OPENROUTER_API_KEY=sk-or-... \
POSTGRES_DSN=postgres://user:pass@host/db \
python demo.py
```

Dependencies: `pip install pydantic-ai-slim[openai] bashkit asyncpg`

### Summary

|                    | bashkit                     | TigerFS                 |
| ------------------ | --------------------------- | ----------------------- |
| Layer              | Rust in-process (HashMap)   | Kernel FUSE/NFS daemon  |
| Scope              | Single process, per-session | OS-wide, multi-process  |
| Persistence        | Ephemeral (lost on reset)   | ACID Postgres rows      |
| Concurrency        | Per-instance isolation      | Shared, concurrent-safe |
| Direct composition | —                           | —                       |

**Verdict:** not directly composable, but the sync bridge via asyncpg gives you the webapp architecture you described — many concurrent users, no per-user VMs or containers, durable state between turns, and human/agent interop through the same Postgres database TigerFS exposes as a filesystem.

---

## Part 2: Monty + TigerFS

A proof-of-concept showing that **pydantic Monty** (sandboxed Python interpreter
written in Rust) can be given a **postgres-backed filesystem** via
**TigerFS**, orchestrated by a **pydantic-ai** agent using **OpenRouter** as
the model provider.

### The short answer: yes, it works — and the fit is clean

Monty's design already anticipates this:

> "Completely block access to the host environment: filesystem, env variables
> and network access are all implemented via **external function calls the
> developer can control**."

TigerFS exposes a standard POSIX filesystem backed by PostgreSQL (ACID transactions, version history, concurrent access).
You bridge the two with a thin Python wrapper that implements `read_file`, `write_file`, and `list_dir`, then pass those as Monty's `external_functions`.
The sandbox never touches the host; every file operation goes:

```text
Monty code
  └─▶ external_functions dict  (your Python bridge)
        └─▶ standard open() / pathlib calls on /mnt/db/...
              └─▶ FUSE/NFS  ──▶  TigerFS daemon  ──▶  PostgreSQL
```

Unlike bashkit's `FileSystem` trait (Part 1 above), Monty has no filesystem abstraction of its own to reconcile with TigerFS's FUSE layer — it has _no_ filesystem access by design, so every path goes through the developer-supplied bridge anyway.
That's why Monty composes with TigerFS more directly than bashkit does.

(Full fact sheets: [`vfs-landscape-and-fit.md`](vfs-landscape-and-fit.md) §1.3 (Monty) and §1.1 (TigerFS).)

### Architecture

```text
┌──────────────────────────────────────┐
│  pydantic-ai Agent  (OpenRouter)     │
│  model: any OpenRouter model string  │
└───────────────┬──────────────────────┘
                │  run_code(code) tool call
┌───────────────▼──────────────────────┐
│  Monty sandbox  (Rust bytecode VM)   │
│  • zero host access by default       │
│  • read_file / write_file / list_dir │
│    provided as external_functions    │
└───────────────┬──────────────────────┘
                │  Python callbacks
┌───────────────▼──────────────────────┐
│  TigerFSBridge  (thin Python class)  │
│  resolves sandbox paths to mount     │
│  prevents path-traversal escapes     │
└───────────────┬──────────────────────┘
                │  POSIX I/O on /mnt/db
┌───────────────▼──────────────────────┐
│  TigerFS FUSE/NFS mount              │
│  tigerfs mount postgres://... /mnt/db│
└───────────────┬──────────────────────┘
                │
┌───────────────▼──────────────────────┐
│  PostgreSQL                          │
│  • ACID writes  • version history    │
│  • concurrent agent access           │
└──────────────────────────────────────┘
```

### Requirements

| Package                         | Install                                       |
| ------------------------------- | --------------------------------------------- |
| `pydantic-monty`                | `pip install pydantic-monty`                  |
| `pydantic-ai`                   | `pip install pydantic-ai`                     |
| `openai` (OpenAI-compat client) | `pip install openai`                          |
| TigerFS CLI                     | `curl -fsSL https://install.tigerfs.io \| sh` |

### Running the demo

#### 1 — no DB, no API key (demo mode, in-memory fallback)

```bash
pip install pydantic-monty pydantic-ai openai
python monty_tigerfs_demo.py
```

The standalone Monty ↔ filesystem demo runs immediately using an in-memory dict instead of a real postgres mount.
The agent section is skipped if `OPENROUTER_API_KEY` is not set.

#### 2 — with TigerFS (real Postgres-backed FS)

```bash
# mount
tigerfs mount postgres://localhost/agentdb /mnt/db

# run
TIGERFS_MOUNT=/mnt/db DEMO_MODE=0 python monty_tigerfs_demo.py
```

#### 3 — full stack (TigerFS + OpenRouter agent)

```bash
export OPENROUTER_API_KEY=sk-or-...
export TIGERFS_MOUNT=/mnt/db
export DEMO_MODE=0
python monty_tigerfs_demo.py
```

### What the demo exercises

| Step              | What happens                                                                    |
| ----------------- | ------------------------------------------------------------------------------- |
| Write from Monty  | Sandbox calls `write_file(path, content)` → TigerFS → Postgres row insert       |
| Read back         | Sandbox calls `read_file(path)` → TigerFS → Postgres row select                 |
| Directory listing | `list_dir(path)` → TigerFS → Postgres query                                     |
| Atomic increment  | Write → read → write → read in a single Monty execution shows state persistence |
| Agent loop        | pydantic-ai agent asks Monty to write a report, read it back, list the dir      |

### Key design notes

#### Path safety

`TigerFSBridge._resolve()` uses `Path.resolve()` plus
`.relative_to(mount)` to prevent sandbox code from escaping via `../` tricks.

#### Choosing the model

The demo uses `"anthropic/claude-3.5-sonnet"` via OpenRouter.
You can swap in any OpenRouter model string, e.g.:

```python
model_name = "openai/gpt-4o"
model_name = "google/gemini-2.0-flash"
model_name = "deepseek/deepseek-r1"
```

#### Monty limitations (as of v0.0.8)

- No `class` definitions
- No `match` statements
- No `with` / context managers
- No standard library (`os`, `pathlib`, etc.) — but you don't need them;
  the bridge functions are the only FS interface Monty gets

#### Why TigerFS beats alternatives for agents

| Option       | Problem                                           |
| ------------ | ------------------------------------------------- |
| Local files  | No ACID, no concurrent access, no version history |
| Git          | Requires pull/push/merge; not real-time           |
| S3           | No transactions; eventual consistency             |
| Raw Postgres | Agents need schema knowledge; no file-like API    |
| **TigerFS**  | POSIX API + ACID + history + concurrent agents ✓  |

### Conclusion

The integration is clean because both Monty and TigerFS are designed around the same principle: **explicit, auditable access**.
Monty forces all filesystem access through developer-supplied callbacks.
TigerFS makes every callback a postgres transaction.
The bridge between them is ~30 lines of Python.
