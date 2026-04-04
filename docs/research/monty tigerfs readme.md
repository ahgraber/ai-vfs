# monty + tigerfs demo

A proof-of-concept showing that **pydantic Monty** (sandboxed Python interpreter
written in Rust) can be given a **postgres-backed filesystem** via
**TigerFS**, orchestrated by a **pydantic-ai** agent using **OpenRouter** as
the model provider.

---

## The short answer: yes, it works — and the fit is clean

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

---

## Architecture

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

---

## Requirements

| Package                         | Install                                       |
| ------------------------------- | --------------------------------------------- |
| `pydantic-monty`                | `pip install pydantic-monty`                  |
| `pydantic-ai`                   | `pip install pydantic-ai`                     |
| `openai` (OpenAI-compat client) | `pip install openai`                          |
| TigerFS CLI                     | `curl -fsSL https://install.tigerfs.io \| sh` |

---

## Running the demo

### 1 — no DB, no API key (demo mode, in-memory fallback)

```bash
pip install pydantic-monty pydantic-ai openai
python monty_tigerfs_demo.py
```

The standalone Monty ↔ filesystem demo runs immediately using an in-memory dict instead of a real postgres mount.
The agent section is skipped if `OPENROUTER_API_KEY` is not set.

### 2 — with TigerFS (real Postgres-backed FS)

```bash
# mount
tigerfs mount postgres://localhost/agentdb /mnt/db

# run
TIGERFS_MOUNT=/mnt/db DEMO_MODE=0 python monty_tigerfs_demo.py
```

### 3 — full stack (TigerFS + OpenRouter agent)

```bash
export OPENROUTER_API_KEY=sk-or-...
export TIGERFS_MOUNT=/mnt/db
export DEMO_MODE=0
python monty_tigerfs_demo.py
```

---

## What the demo exercises

| Step              | What happens                                                                    |
| ----------------- | ------------------------------------------------------------------------------- |
| Write from Monty  | Sandbox calls `write_file(path, content)` → TigerFS → Postgres row insert       |
| Read back         | Sandbox calls `read_file(path)` → TigerFS → Postgres row select                 |
| Directory listing | `list_dir(path)` → TigerFS → Postgres query                                     |
| Atomic increment  | Write → read → write → read in a single Monty execution shows state persistence |
| Agent loop        | pydantic-ai agent asks Monty to write a report, read it back, list the dir      |

---

## Key design notes

### Path safety

`TigerFSBridge._resolve()` uses `Path.resolve()` plus
`.relative_to(mount)` to prevent sandbox code from escaping via `../` tricks.

### Choosing the model

The demo uses `"anthropic/claude-3.5-sonnet"` via OpenRouter.
You can swap in any OpenRouter model string, e.g.:

```python
model_name = "openai/gpt-4o"
model_name = "google/gemini-2.0-flash"
model_name = "deepseek/deepseek-r1"
```

### Monty limitations (as of v0.0.8)

- No `class` definitions
- No `match` statements
- No `with` / context managers
- No standard library (`os`, `pathlib`, etc.) — but you don't need them;
  the bridge functions are the only FS interface Monty gets

### Why TigerFS beats alternatives for agents

| Option       | Problem                                           |
| ------------ | ------------------------------------------------- |
| Local files  | No ACID, no concurrent access, no version history |
| Git          | Requires pull/push/merge; not real-time           |
| S3           | No transactions; eventual consistency             |
| Raw Postgres | Agents need schema knowledge; no file-like API    |
| **TigerFS**  | POSIX API + ACID + history + concurrent agents ✓  |

---

## Conclusion

The integration is clean because both Monty and TigerFS are designed around the same principle: **explicit, auditable access**.
Monty forces all filesystem access through developer-supplied callbacks.
TigerFS makes every callback a postgres transaction.
The bridge between them is ~30 lines of Python.
