# Sandboxed Multi-Tenant Code Execution Research

_Conversation exported April 04, 2026_

---

## Context

Research conversation exploring the architecture of a cloud-hosted ChatGPT-style web app that gives users Claude Code-style file interaction (search, read, edit, create) with sandboxed code execution — without OS-level isolation (no containers, VMs, seccomp, etc.).
The constraint is interpreter-level or in-process isolation only.

---

## Turn 1 — Initial framing

**User:**

> I want to build a cloud-hosted chatgpt-style webapp that effectively provides users the power and expressiveness of claude code (native code writing over a filesystem), but running as a webapp with multiple concurrent users.
>
> I had been thinking of tigerfs or juicefs to support the virtual filesystem, and then something like everruns/bashkit, pydantic monty, etc. as a minimal, tightly-scoped code virtual environment.
>
> After some experimentation, the bashkit + tigerFS doesn't quite get what I want because they don't integrate directly, but it comes quite close in theory.
>
> Continue this line of thinking and identify viable tooling (python ecosystem, preferably) that supports exposing cloud-native services as virtual filesystems and minimal python, typescript, or bash interpreter sandboxes that do not require granting control of a full sandbox environment, VM, or container (e.g., no E2B, Modal, etc).

**Clarifying questions asked:**

- Primary workload to sandbox: File I/O heavy (read/write cloud storage), Bash/shell commands, Python execution — with file search, read, edit, and create as primary user actions; also "codemode" tool-calling execution (per Pydantic Monty and Cloudflare Code Mode)
- Cloud storage backends: S3/S3-compatible, Azure Blob, Postgres/Document DB (CosmosDB, Mongo)
- Security/isolation priority: Strong isolation per user (untrusted code)

---

## Turn 2 — First research report

_(Full report: "Building a Sandboxed Multi-Tenant Code Execution Platform in Python")_

### Key findings

**The defining constraint:** "Start from nothing, allowlist capabilities" — not "start from a real runtime, restrict it."
Pydantic Monty and Bashkit embody this; OS-level approaches (nsjail, bubblewrap, etc.) do not.

**Virtual filesystem layer — fsspec dominates:**

- **fsspec + DirFileSystem**: The clear winner.
  Built-in `DirFileSystem` wraps any backend with a path prefix for per-user isolation — no FUSE required.
  Backends: `s3fs` (S3/MinIO), `adlfs` (Azure Blob/ADLS Gen2), `gcsfs` (GCS).
  Monthly releases, used by pandas/Dask/HuggingFace.
- **PyFilesystem2 + SubFS**: Clean Python API but stalled maintenance (last release 2023).
- **JuiceFS**: Overkill; requires Redis/Postgres metadata engine; designed for petabyte-scale.
- **TigerFS**: Postgres-backed FUSE filesystem, no Python SDK, too early-stage.
- No tool handles Postgres/MongoDB as a filesystem natively — expose via registered tool functions instead.

**Code execution sandbox — OS-level isolation is non-negotiable (or is it?):**

_This section was later corrected — see Turn 3._

**The codemode/CodeAct pattern:**

The LLM tool-call loop: LLM emits code → router dispatches to Monty (Python) or Bashkit (Bash) → result returns as tool result → LLM continues.
The CodeAct pattern (ICML 2024) showed 20% higher success rates and 30% fewer interaction steps vs. JSON tool calls.

**Three concrete integration architectures proposed:**

1. **Lightweight Linux namespace isolation** (bwrap + seccomp + Landlock) — ❌ Violates the no-OS-primitives constraint
2. **In-process embedded interpreter** (Pydantic Monty + fsspec external functions) — ✅ Fits
3. **Hybrid** — Monty for fast tool calls + bwrap for full execution — ❌ Partially violates constraint

---

## Turn 3 — Correction and focused re-research

**User:**

> You did zero comparisons or research for bash-equivalents to monty (like the suggested bashkit). Further, the majority of the "sandbox" solutions require OS level protections; this will be running as a webapp and we cannot provision distinct sandboxes for each user. The point was to AVOID sandboxes by using innovative tooling like bashkit or monty.

**Corrected analysis — the interpreter family:**

### Bashkit (everruns/bashkit)

- POSIX-compliant Bash interpreter written in **Rust**, designed explicitly for multi-tenant environments
- In-process execution — no spawned processes, no OS calls by default
- **Virtual filesystem built in**: `InMemoryFs`, `OverlayFs`, `MountableFs` (all Rust traits)
- Resource limits: command count, loop iterations, function depth
- Network allowlist: HTTP access scoped per-domain
- **Python bindings**: `bashkit-python` (PyPI) with LangChain integration
- **Embeds Monty**: Bashkit supports running Python via Monty as an experimental feature — the two are designed to compose
- Gap: `MountableFs` doesn't directly integrate with S3/Azure, but `MountableFs` accepts `Arc<dyn FileSystem>`, so a custom Rust backend wrapping `object_store` would bridge this (~500 lines of Rust)

### just-bash (vercel-labs/just-bash)

- TypeScript equivalent of Bashkit — same philosophy, different language
- Runs in-process in Node.js or the browser
- `IFileSystem` interface you can implement; `Sandbox` class is API-compatible with `@vercel/sandbox`
- Python via Pyodide (WASM) available but carries CVE-2025-68668 caveat

### Pydantic Monty (pydantic/monty)

- Rust-based Python subset VM, zero I/O by default
- External functions: register fsspec-backed `read_file`, `write_file`, `list_dir` as Monty external functions
- Filesystem from Monty's perspective is purely your Python code — backed by fsspec `DirFileSystem`
- No third-party imports, no classes — orchestration layer, not a full Python REPL

### Recommended stack

```text
User session
    │
    ├── Bash-like operations ──► Bashkit (Rust, in-process)
    │                                └── MountableFs backed by object_store
    │                                     ├── /workspace → S3 prefix per user
    │                                     ├── /data → Azure Blob
    │                                     └── /db → custom Postgres trait impl
    │
    └── Python execution ──────► Monty (Rust VM, in-process)
                                     └── external functions → fsspec DirFileSystem
                                          ├── s3fs (S3)
                                          ├── adlfs (Azure Blob)
                                          └── motor/pymongo (documents)
```

Both run in the same Python process.
Neither spawns subprocesses.
Neither touches the host filesystem by default.
Per-user isolation is entirely application-layer path namespacing.

**Honest gaps:**

- Bashkit `MountableFs` ↔ cloud storage integration doesn't exist yet (~1–2 weeks of Rust work)
- Bashkit is new (30 stars at the time)
- Monty's Python subset: no third-party imports, no classes, no generators — orchestration only, not a full Python REPL

---

## Turn 4 — Broader interpreted environment research

**User:**

> Do additional research. What other interpreted environments and solutions exist. I'm looking for solutions similar to pydantic monty, bashkit, or (possibly?) WASM + deno(?)

_(Full report: "In-Process Sandboxed Execution Environments for Multi-Tenant Code: 2026 Strategic Analysis")_

### Key finding: WASM is the only approach blocking all six security vectors in-process

The Arize Phoenix team empirically tested six vectors (memory isolation, env-var exfiltration, outbound networking, filesystem reads, subprocess spawning, CPU/memory exhaustion) across every major sandbox backend.
CPython compiled to WASM running inside wasmtime was the only no-sidecar option passing all six.

---

### WASM runtimes

| Tool                     | Status                                     | Python API                           | Notes                                                                                                                         |
| ------------------------ | ------------------------------------------ | ------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------- |
| **wasmtime-py**          | ✅ Actively maintained (v41.0.0, Jan 2026) | PyPI `wasmtime`                      | Fuel metering, epoch interruption, memory limits, WASI preopens. Best raw option.                                             |
| **Eryx** (eryx-org/eryx) | ✅ New but purpose-built                   | PyPI `pyeryx`                        | CPython 3.14 in WASM. ~16ms sandbox creation with AOT. Pooling, pre-init, session state, MCP support. Most complete solution. |
| **wasmer-py**            | ❌ Dead                                    | Python 2022, incompatible with 3.11+ | Wasmer pivoted to cloud/edge                                                                                                  |
| **WAMR**                 | ⚠️ Experimental                            | Undocumented Python bindings         | Not on PyPI                                                                                                                   |
| **Extism**               | ✅ Active                                  | PyPI `extism`                        | Higher-level WASM plugin abstraction; lacks direct fuel metering                                                              |

**Pyodide status post-CVE-2025-68668:**

- CVE-2025-68668 (CVSS 9.9): Pyodide alone provides no security boundary on a server
- Attack vector: `_pyodide._base.eval_code()` + ctypes-like indirection → `system()` without touching `os.system`
- Grist-Core had a similar escape (CVSS 9.1)
- **Pyodide + Deno** is the safe pattern: Deno's `--deny-*` flags restrict the process; Pyodide runs inside V8
- Used by: LangChain `langchain-sandbox`, Pydantic `mcp-run-python`, HuggingFace smolagents `WasmExecutor`, Cloudflare Python Workers
- Downside: spawns a Deno subprocess, not truly in-process

---

### V8 isolates from Python

| Tool                                   | Status                                    | Python API            | Notes                                                                             |
| -------------------------------------- | ----------------------------------------- | --------------------- | --------------------------------------------------------------------------------- |
| **PyMiniRacer** (bpcreech/PyMiniRacer) | ✅ Actively maintained (revived Mar 2024) | PyPI `mini-racer`     | V8 14.4, one isolate per `MiniRacer()`, hard heap limits, eval timeouts. JS only. |
| **QuickJS** (PetterS/quickjs)          | ⚠️ Archived Jan 2026                      | PyPI `quickjs`        | Lighter (~600KB), built-in memory/time limits. QuickJS-NG fork is active.         |
| **QuickJS-NG**                         | ✅ Active (16 releases through Feb 2026)  | No Python binding yet | Community-maintained fork                                                         |

**Caveat on V8 isolates:** V8 has had numerous sandbox escape CVEs.
Cloudflare layers process isolation + kernel features + rapid patching on top of workerd's V8 isolates.
The open-source workerd README explicitly warns it "does NOT contain suitable defense-in-depth against implementation bugs."

---

### Language-specific VMs

| Tool                                 | Language executed | Python API                        | Isolation                                              | Notes                                                   |
| ------------------------------------ | ----------------- | --------------------------------- | ------------------------------------------------------ | ------------------------------------------------------- |
| **Starlark** (starlark-pyo3)         | Python dialect    | PyPI `starlark-pyo3`              | Hermetic by design — no I/O, no network, deterministic | No loops, no classes, guaranteed termination            |
| **Lua via lupa**                     | Lua 5.4 or LuaJIT | PyPI `lupa` v2.6                  | Manual whitelist (remove `io`, `os`, `debug`)          | Use PUC-Rio Lua 5.4, not LuaJIT, for instruction limits |
| **CEL** (Common Expression Language) | CEL               | PyPI `common-expression-language` | Non-Turing-complete, side-effect free                  | Google announced `cel-expr-python` March 2026           |
| **Boa**                              | JavaScript        | Rust only                         | Embeddable JS in Rust                                  | No Python bindings                                      |
| **Rhai**                             | Rhai scripting    | Rust only                         | Configurable memory/CPU limits                         | No Python bindings                                      |

---

### Python-level sandboxing — fundamentally broken

Every CPython core developer agrees: "Run the Python process in a sandbox, don't run a sandbox in Python."

- **PEP 578 audit hooks** (`sys.addaudithook()`): explicitly not a sandbox — the PEP says so
- **Python subinterpreters** (PEP 684/734): per-interpreter GILs for concurrency, zero security isolation
- **RestrictedPython**: acknowledges in its own docs it "is not a sandbox system"
- **Import restrictions**: trivially bypassed via `__subclasses__()` chains

---

### Tiered execution model (recommended)

```text
Tier 1 — Expression evaluation (<1ms)
  → Starlark (starlark-pyo3) or CEL
  → For: simple expressions, data filtering, policy rules
  → Guaranteed termination, zero I/O, hermetic

Tier 2 — In-process interpreted execution (µs–ms)
  → Pydantic Monty (Python subset) or QuickJS/PyMiniRacer (JS)
  → For: high-volume LLM tool-call orchestration
  → Fast path, NOT a security boundary for adversarial code

Tier 3 — WASM-sandboxed full Python (tens of ms)
  → Eryx (preferred) or wasmtime-py + CPython.wasm
  → Only in-process option blocking all 6 security vectors
  → Fuel metering, WASI preopens, linear memory isolation

Fallback — Cloud sidecar (hundreds of ms)
  → E2B or self-hosted Firecracker/Docker
  → For: code requiring pip packages (NumPy, Pandas, requests)
```

---

## Turn 5 — Filesystem integration deep dive

**User:**

> How would fsspec or tigerfs integrate with the top options?

_(Full report: "How fsspec and TigerFS Connect to In-Process Code Sandboxes: Integration Patterns and Roadmap")_

### The three integration patterns

Every sandbox studied falls into one of three groups based on how a filesystem layer can reach inside it.

---

### Pattern 1 — Function injection (Monty, Starlark, PyMiniRacer, QuickJS)

These sandboxes have zero I/O by default and expose a mechanism to register host-side callable functions as globals inside the sandbox.
Works today, pure Python.

**Monty — cleanest integration:**

```python
fs = fsspec.filesystem("s3", key="...", secret="...")
fs_funcs = {
    "file_read": lambda path: fs.cat(path).decode("utf-8"),
    "file_write": lambda path, data: fs.pipe(path, data.encode("utf-8")),
    "file_list": lambda path: fs.ls(path, detail=False),
}
# pydantic-monty 0.0.18: async, and `os=` mounts a native filesystem.
m = pydantic_monty.Monty(code)
result = await m.run_async(external_functions=fs_funcs, os=mount)
```

Monty's **snapshot capability** is particularly valuable: a sandbox can pause mid-execution when it needs a file, serialize its state, and resume when the I/O completes — enabling non-blocking, event-driven execution across many tenants.

**Starlark:** Uses `module.add_callable(name, callable)`.
Values pass through JSON serialization (no bytes type; binary must be base64-encoded).
No exception handling — errors terminate evaluation entirely.

**PyMiniRacer:** `wrap_py_function()` injects Python functions as **async JavaScript Promises** — V8's single-threaded architecture means synchronous callbacks would deadlock.
Has native bytes-to-ArrayBuffer conversion (only JS sandbox supporting binary files without base64).

**QuickJS:** Simpler synchronous `add_callable(name, func)` but archived January 2026.

---

### Pattern 2 — Filesystem trait implementation (Bashkit, just-bash)

These sandboxes define a formal filesystem interface.
Cloud-backing requires implementing the trait against a cloud SDK.

**Bashkit:**

- Exports a `FileSystem` trait used as `Arc<dyn FileSystem>` with `InMemoryFs`, `OverlayFs`, `MountableFs`
- Cloud integration: implement the `FileSystem` trait in Rust, backed by `object_store` crate (Apache Arrow, supports S3/Azure/GCS) or Apache OpenDAL (40+ storage backends)
- Bashkit's tokio-based async architecture aligns with these async Rust storage crates
- Staging pattern (works today without Rust): fsspec downloads to temp dir → Bashkit reads from temp dir → sync back

**just-bash:**

- `IFileSystem` TypeScript interface with same architecture as Bashkit
- Cloud-backing: implement `IFileSystem` in TypeScript against `@aws-sdk/client-s3`
- `MountableFs` supports `.mount()` calls — cloud-backed FS at `/cloud`, `InMemoryFs` at `/tmp`
- Simplest approach: stage files as `Record<string, string>`, pass as `new Bash({ files })`, sync results
- **Limitation:** `InMemoryFs` constrained by Node.js heap (~1.5GB); 10MB max file read default

---

### Pattern 3 — WASI capability mapping (Eryx, wasmtime-py)

**How WASI preopened directories work:** WASM modules start with zero filesystem access.
The host grants access by "preopening" specific directories.
`WasiConfig.preopen_dir(host_path, guest_path)` accepts **only real OS filesystem paths** — there is no Python-level mechanism to substitute a virtual filesystem.

**Three approaches for WASI sandboxes:**

1. **Staging pattern** (works today, Python-only):

   - fsspec downloads files to `tempfile.TemporaryDirectory()`
   - Call `preopen_dir(tmpdir, "/data")`
   - Run WASM module
   - Sync modified files back via fsspec
   - Simple and reliable; requires full download before execution

2. **FUSE mount pattern** (requires FUSE support):

   - fsspec has experimental `fsspec.fuse.run()` that mounts any fsspec filesystem as FUSE
   - `s3fs-fuse` provides a mature S3 FUSE mount
   - Point `preopen_dir()` at the mount point for lazy-loading
   - **Unavailable in many container environments without privileged access**

3. **Custom Rust `WasiDir`/`WasiFile`** (most powerful, significant effort):

   - wasmtime's `wasi-common` crate explicitly documents this extensibility point
   - Implement `WasiDir` and `WasiFile` traits backed by cloud storage
   - Standard Python `open()` and `os.listdir()` inside the sandbox route transparently
   - Cannot be driven from Python — requires a custom Rust crate or extending wasmtime-py

**Eryx-specific:** Callback-based interaction — the host defines typed async callbacks that sandboxed Python calls via `await`.
Two paths:

- **Callback-based (Eryx-native)**: `content = await read_file("/data/file.txt")` → host maps to `fsspec.open(...)`
- **WASI-level**: Implement custom Rust traits with transparent `open()` routing

---

### TigerFS assessment

TigerFS (tigerfs.io) — a Postgres-backed virtual filesystem by Tiger Data (formerly Timescale):

- **No Python API, no PyPI package, no in-process library mode**
- Entire API surface is the filesystem itself (FUSE mount only)
- No public GitHub repository (appears to be private/early access)
- No direct S3/cloud object storage support — backend is exclusively PostgreSQL
- **Not usable in containerized environments without FUSE/privileged access**
- Multi-tenant isolation: only standard Postgres permissions
- Integration with sandboxes: mount-then-point only (`preopen_dir("/mnt/tigerfs", "/data")`)
- **Important disambiguation**: TigerFS (Tiger Data/Timescale) is unrelated to TigrisFS (Tigris Data, S3-compatible FUSE adapter)

---

### WASI virtual filesystem implementations compared

| Implementation                   | Runtime             | Dynamic backend             | Python-accessible | Maturity                                   |
| -------------------------------- | ------------------- | --------------------------- | ----------------- | ------------------------------------------ |
| **wasi-common WasiDir/WasiFile** | Wasmtime            | Yes (custom Rust)           | Rust only         | Mature, designed for this                  |
| **wasi-vfs**                     | Any WASI            | No (build-time embed)       | CLI tool          | Stable (v0.5.5)                            |
| **WASI-Virt**                    | Wasmtime components | No (post-compile)           | CLI tool          | Early (154 stars)                          |
| **Wasmer virtual-fs**            | Wasmer only         | Yes (custom Rust)           | No                | Active, Wasmer-only                        |
| **tmpdir staging**               | Any                 | Yes (via fsspec)            | Python            | Production-ready                           |
| **FUSE mount**                   | Any                 | Yes (s3fs-fuse/fsspec.fuse) | Python            | s3fs-fuse mature; fsspec.fuse experimental |

---

### Existing adjacent projects

| Project                           | Architecture                                                                                | Relevance                                            |
| --------------------------------- | ------------------------------------------------------------------------------------------- | ---------------------------------------------------- |
| **AgentFS** (Turso/tursodatabase) | SQLite-backed virtual FS for AI agents; FUSE mounting; copy-on-write overlays; WASM support | Most directly relevant architectural pattern         |
| **Localsandbox** (CoPlane)        | AgentFS + just-bash + Pyodide combined                                                      | Closest existing project to the desired architecture |
| **llm-wasm-sandbox** (PyPI)       | WASM sandbox for LLM-generated Python/JS with pluggable storage adapter interface           | Production-grade, closest to described use case      |
| **E2B**                           | Firecracker microVMs, ~150ms creation                                                       | Cloud sidecar, not in-process                        |
| **Modal**                         | gVisor containers, filesystem snapshots                                                     | Cloud sidecar, not in-process                        |

---

### Integration roadmap

**Immediate (Python-only, works today):** Build a thin `FsspecBridge` wrapping an fsspec filesystem instance, exposing `read(path)`, `write(path, str)`, `list(path)`, `exists(path)` as injectable functions. ~50 lines of Python per sandbox.
Use staging pattern for WASI sandboxes.

**Medium-term (Rust/TypeScript, enables live cloud access):**

- Bashkit: implement `FileSystem` trait backed by `object_store` crate
- just-bash: implement `IFileSystem` backed by `@aws-sdk/client-s3`
- Eryx: implement filesystem operations as `TypedCallback` traits bridging to fsspec

**Long-term (maximum capability):** Implement custom `WasiDir`/`WasiFile` Rust traits backed by cloud storage with caching layer.
Extend wasmtime-py via PyO3 to expose the virtual filesystem system to Python.

---

## Summary: Full tool landscape

### Bash/shell interpreters (in-process, no OS required)

| Tool          | Language   | In-process | VFS                                             | Python API              | Status          |
| ------------- | ---------- | ---------- | ----------------------------------------------- | ----------------------- | --------------- |
| **Bashkit**   | Rust       | ✅         | InMemoryFs, OverlayFs, MountableFs              | PyPI `bashkit-python`   | Active (v0.1.4) |
| **just-bash** | TypeScript | ✅         | InMemoryFs, OverlayFs, MountableFs, ReadWriteFs | npm `just-bash` v2.12.8 | Active          |

### Python execution (in-process, no OS required)

| Tool                           | Isolation mechanism                | Full CPython     | Python API            | Cloud FS               | Status                       |
| ------------------------------ | ---------------------------------- | ---------------- | --------------------- | ---------------------- | ---------------------------- |
| **Pydantic Monty**             | Rust bytecode VM, no I/O by design | ❌ subset        | PyPI `pydantic-monty` | Via external functions | Active (6.4k stars)          |
| **Eryx**                       | CPython 3.14 in WASI/Wasmtime      | ✅               | PyPI `pyeryx`         | Via callbacks or WASI  | New (41 stars)               |
| **wasmtime-py + CPython.wasm** | WASM linear memory isolation       | ✅ (stdlib only) | PyPI `wasmtime`       | Via staging/FUSE       | Active (v41.0.0)             |
| **Pyodide + Deno**             | Deno permissions + WASM            | ✅               | Subprocess only       | Via staging            | Viable (CVE risk in Node.js) |

### JavaScript execution (in-process)

| Tool            | Python API        | Memory limits  | Binary support  | Status                |
| --------------- | ----------------- | -------------- | --------------- | --------------------- |
| **PyMiniRacer** | PyPI `mini-racer` | ✅ Hard limits | ✅ ArrayBuffer  | Active (revived 2024) |
| **QuickJS**     | PyPI `quickjs`    | ✅ Built-in    | ❌ Strings only | Archived Jan 2026     |

### Constrained expression/scripting languages

| Tool             | Python API                        | Termination              | I/O                | Status                    |
| ---------------- | --------------------------------- | ------------------------ | ------------------ | ------------------------- |
| **Starlark**     | PyPI `starlark-pyo3`              | ✅ Guaranteed            | ❌ None by default | Active                    |
| **CEL**          | PyPI `common-expression-language` | ✅ Guaranteed            | ❌ None            | Active (Google, Mar 2026) |
| **Lua via lupa** | PyPI `lupa` v2.6                  | ✅ Via instruction count | Manual whitelist   | Active                    |

### Virtual filesystem layer

| Tool                       | In-process    | S3         | Azure Blob   | Per-user isolation | Status                |
| -------------------------- | ------------- | ---------- | ------------ | ------------------ | --------------------- |
| **fsspec + DirFileSystem** | ✅            | ✅ s3fs    | ✅ adlfs     | ✅ path prefix     | Very active           |
| **PyFilesystem2 + SubFS**  | ✅            | ✅ fs-s3fs | ⚠️ 3rd party | ✅ SubFS           | Stalled (2023)        |
| **object_store** (Rust)    | ✅            | ✅         | ✅           | App-level          | Active (Arrow)        |
| **Apache OpenDAL** (Rust)  | ✅            | ✅         | ✅           | App-level          | Active (40+ backends) |
| **TigerFS**                | ❌ FUSE only  | ❌         | ❌           | Postgres only      | Early access          |
| **AgentFS**                | ❌ FUSE / SDK | ❌         | ❌           | SQLite per-agent   | Early                 |

---

## Turn 6 — OPFS + Pyodide/WASM: client-side inversion

**User:**

> How might OPFS and Pyodide/WASM change this?
> References: Simon Willison's tools.simonwillison.net colophon; MDN File System API / Origin Private File System

### Key finding: moving the sandbox to the client dissolves the server-side isolation problem

The research's three core tensions were:

1. WASI preopened directories require real OS paths — no Python-level virtual filesystem substitution
2. Pyodide on a server carries CVE-2025-68668 (CVSS 9.9) — not a security boundary
3. The staging pattern forces full file download before execution

OPFS (Origin Private File System) + Pyodide running in a browser Worker resolves all three by shifting execution to the client.

---

### OPFS: what it provides

OPFS is the sandboxed, origin-scoped partition of the File System Access API.
Key properties for this use case:

- **`createSyncAccessHandle()`** (Workers only): Synchronous byte-level read/write with no permission prompts — exactly what Emscripten's POSIX layer needs to emulate `open()`/`read()`/`write()` inside Pyodide.
  Standard Python `open()` calls work without any staging layer.
- **Persistent across sessions**: Packages are written to OPFS on first load and skipped on subsequent loads.
  Simon Willison's tools use this pattern to amortize the ~7MB Pyodide download.
- **Origin-scoped isolation**: Each browser origin gets a completely separate OPFS partition.
  Per-user isolation is free — no application-layer path namespacing required.

Pyodide exposes this via `pyodide.mountOPFS()`, making the OPFS partition available as a standard filesystem path inside the Python interpreter.

---

### CVE-2025-68668 reassessment

The CVE is a server-side concern.
The attack vector (`_pyodide._base.eval_code()` + ctypes indirection → `system()`) is dangerous on a server because the Pyodide process has OS access.

In the browser, Pyodide running inside a Worker is running in its _intended_ deployment target.
The browser's same-origin policy, process isolation, and Worker sandbox are the security boundary — not Pyodide itself.
The "Pyodide + Deno subprocess" workaround the earlier research recommended is unnecessary in the browser context.

---

### The architecture inverts

The prior research assumed server-side tiers:

```text
Server process
    └── in-process sandbox (Monty / Eryx / wasmtime)
         └── staged files ──► fsspec ──► cloud storage
```

OPFS + Pyodide enables a client-side execution tier:

```text
Browser Worker
    └── Pyodide (full CPython in WASM)
         └── OPFS (persistent, synchronous) ◄──fetch──► server API ──► cloud storage
```

The server no longer needs an in-process code sandbox at all.
It becomes a thin, authenticated cloud-storage proxy.
Per-user sandbox isolation is provided by the browser; per-user storage isolation is provided by the existing fsspec + DirFileSystem layer on the server.

---

### Revised tier model

| Tier     | Runtime        | Execution engine                                  | Filesystem                         | Security boundary            |
| -------- | -------------- | ------------------------------------------------- | ---------------------------------- | ---------------------------- |
| 1        | Browser Worker | Starlark / CEL — expression eval                  | N/A (no I/O)                       | Hermetic by design           |
| 2        | Browser Worker | Pyodide + OPFS — full Python, persistent packages | OPFS (sync access handle)          | Browser origin sandbox       |
| Fallback | Server         | Eryx / wasmtime-py                                | Staging or custom Rust WASI traits | WASM linear memory isolation |

Tier 2 now replaces both Monty (Tier 2 in the old model) and Eryx/wasmtime (Tier 3) for browser-hosted deployments.
Eryx/wasmtime remains the right choice for headless or server-only execution paths.

---

### Constraints that remain

- **Browser-only:** OPFS is unavailable outside the browser.
  Server-side runtimes (Node, Deno) lack native OPFS; polyfills exist but are unproven at scale.
- **No pip-install of C extensions:** `micropip` fetches only pure-Python wheels from CDN.
  Compiled extensions must be pre-built as Pyodide wheels.
- **Storage quotas:** Browsers allow roughly 60% of available disk per origin but can evict under pressure.
  Applications need quota checks and graceful degradation.
- **Cold-start latency:** Pyodide itself is ~7MB plus packages.
  The OPFS persistence pattern (write install to OPFS, skip download on subsequent loads) is necessary for acceptable UX.
- **CORS constraints:**
  Fetch calls from a Worker are subject to standard CORS; the server API must set appropriate headers.

---

### What does not change

The server-side fsspec + DirFileSystem layer remains the correct answer for durable cloud storage (S3, Azure Blob, GCS).
OPFS becomes the local cache and execution layer; the VFS API endpoints handle sync to/from cloud.
This is a cleaner separation than the staging-in-tmpdir pattern described for wasmtime — the sync boundary is an explicit API call rather than an implicit temp-directory lifecycle.

---

_End of conversation_
