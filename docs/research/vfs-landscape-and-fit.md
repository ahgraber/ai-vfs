# ai-vfs Research: Landscape & Fit

> Everything here is evaluated against the ai-vfs thesis —
> the virtual filesystem as the right substrate for AI agents ([vfs-memo.md](vfs-memo.md)) —
> sharpened by the one property that most differentiates ai-vfs: it targets a
> **multi-tenant B2C + B2B web application** — many organizations and many users
> served concurrently from one shared, stateless backend, with per-request,
> per-path isolation and no per-user VM or container. That lens, not any single
> spec clause, is the yardstick in Part 3.
>
> **Related docs (kept separate on purpose).** [vfs-memo.md](vfs-memo.md) is the
> vision itself; [design-brainstorm-convo.md](design-brainstorm-convo.md) is the
> ai-vfs design decision record that consumed this research; and
> [tigerfs-sandbox-demos.md](tigerfs-sandbox-demos.md) holds the runnable
> bashkit+TigerFS and Monty+TigerFS proof-of-concept code referenced below.

---

## Part 0 — The evaluation lens

### The thesis: filesystem semantics as the interface, storage as the backend

Filesystem semantics are the interface; specialized storage is the backend.
Agents get `read` / `write` / `list` / `search` / `exec` over a uniform namespace, and each path prefix can resolve to a different backend (vector store, object storage, SQL, REST, MCP, code sandbox) without the agent negotiating with any of them.
The full argument — the five things a filesystem uniquely provides, the three independent cost arguments (latency, tokens, composition), the memory-is-the-filesystem-problem insight, and the target product properties — lives in [vfs-memo.md](vfs-memo.md) and is not repeated here.

### The differentiator: multi-tenant B2C + B2B

ai-vfs is not a desktop agent's local working copy.
It is the durable workspace layer for a cloud application serving **thousands of orgs and users from a shared app-server fleet**.
That single fact drives most of the verdicts in Part 3:

- Isolation must be **per-request, per-path, application-layer** — a
  `(namespace_id, principal_id, operation, path)` tuple checked in one Python choke
  point inside a single stateless process — not per-mount, per-VM, or per-container.
- No per-user infrastructure: no VM, no container, no FUSE daemon per tenant.
- Untrusted, agent-generated code runs **in-process** with interpreter-level isolation,
  because provisioning an OS sandbox per user does not scale to web-request concurrency.

### The concrete problem framing (research kickoff)

The research began from a specific goal: build a cloud-hosted, ChatGPT-style web app that gives users the power and expressiveness of Claude Code — native code writing over a filesystem — running with many concurrent users, using the Python ecosystem where possible, and **avoiding** full sandboxes, VMs, or containers (no E2B, Modal, etc.).
The constraint is interpreter-level or in-process isolation only.

Three scoping answers shaped everything downstream:

- **Primary workload to sandbox:** file-I/O-heavy (read/write cloud storage), bash/shell
  commands, and Python execution — with file _search, read, edit, create_ as the
  primary user actions; plus "codemode" tool-calling execution (per Pydantic Monty and
  Cloudflare Code Mode).
- **Cloud storage backends:** S3 / S3-compatible, Azure Blob, and Postgres / document DB
  (Cosmos DB, MongoDB).
- **Security / isolation priority:** strong isolation per user (untrusted code).

---

## Part 1 — The landscape (what exists)

Every technology surveyed appears once, with the union of all facts gathered about it.
Part 2 (analysis) and Part 3 (fit) reference these entries rather than re-describing the tools.

### 1.1 Filesystem-abstraction and virtual-filesystem layers

#### fsspec / filesystem_spec

fsspec (filesystem_spec) is a Python library that provides a unified interface to local, remote, and embedded filesystems.
It is the de facto standard for filesystem abstraction in the Python data ecosystem: Dask, pandas, zarr, and PyArrow all build on it.
Every backend implements the same `AbstractFileSystem` base class, roughly 30 methods (`ls`, `cat`, `mkdir`, `open`, and the rest).
`AbstractBufferedFile` layers random-access reads on top without downloading entire files, which matters for big-data workloads.
Filesystem instances carry no locks or open handles, so they serialize and ship across a Dask cluster.
Layers compose through URL chaining on the `::` separator (`simplecache::zip://*.csv::s3://bucket/file.zip`); caching is built in (file, block, and simple strategies with configurable expiry); transactions cover semi-atomic multi-file writes.
`fsspec.get_mapper()` exposes any path as a dict-like key-value mapping.
`AsyncFileSystem` subclasses expose coroutine methods with auto-generated sync wrappers, already implemented for S3, GCS, and Azure, and any filesystem can be FUSE-mounted through the still-experimental `fsspec.fuse.run()`.

The backend that matters most for ai-vfs is `DirFileSystem`.
It wraps any of these with a path prefix (per-user isolation with no FUSE mount) over `s3fs` (S3/MinIO), `adlfs` (Azure Blob/ADLS Gen2), or `gcsfs` (GCS).
It ships monthly, already backs pandas, Dask, and HuggingFace, and was the clear winner for ai-vfs's VFS layer in this research. fsspec is the most direct foundation available.
Its abstract interface is already the lingua franca for Python filesystem access, so implementing it buys instant compatibility with the Python data stack; its URL-chaining and caching layers are a working demonstration of how to compose virtual-filesystem layers.
JuiceFS already ships an fsspec-compatible SDK (below).

#### PyFilesystem2 + SubFS

PyFilesystem2 offers a clean Python filesystem API with a `SubFS` sub-tree isolation primitive analogous to `DirFileSystem`; backends include `fs-s3fs`, with Azure support only third-party.
Its maintenance has stalled (the last release shipped in 2023), which is why fsspec was preferred over it for ai-vfs.

#### JuiceFS (juicedata/juicefs)

JuiceFS is a high-performance distributed POSIX filesystem, Apache-2.0 and written in Go, that decouples metadata (Redis / PostgreSQL / TiKV / MySQL / SQLite / FoundationDB) from data (any S3-compatible object store: S3, GCS, MinIO, Ceph).

**Architecture.**
It is a "rich client" over two backends: a metadata engine holding POSIX metadata plus the inode → chunk → slice → block mapping, and an object store holding content.
Files split into chunks (≤64 MiB), then slices (individual writes), then blocks (4 MiB default), uploaded as numerically-named objects under a `chunks/` prefix; the original file is not recoverable directly from the bucket.
All I/O, compaction, and trash expiry happen in the client.
Separating metadata from data enables independent scaling: swap Redis for TiKV to handle billions of files, or switch object stores without touching metadata.
JuiceFS is strongly consistent by default with tunable metadata caching; automatic compaction merges fragmented slices, and built-in trash and versioning carry configurable retention.

**Access methods.**
JuiceFS is reachable through a FUSE mount, a Python SDK (fsspec-compatible), a Hadoop Java SDK, a Kubernetes CSI driver, an S3 Gateway, and a WebDAV server.

**The no-FUSE Python SDK (the key finding).**
Community Edition 1.3 (and Enterprise 5.1) ship an official Python SDK, imported as `juicefs` with a `juicefs.Client` class, that performs full file operations with **no FUSE mount and no running gateway**.
It wraps the same Go client compiled to a shared library (`libjfs`, the lib the Java/Hadoop SDK uses) via its C interface: core client code, not a reimplementation or a subprocess.
It connects directly to the same metadata engine and object store a mount would use, skipping the kernel FUSE layer (Community: `juicefs.Client(name="myjfs", meta="redis://localhost")`; Enterprise: `juicefs.Client('volume', token=..., access_key=..., secret_key=...)`).
It exposes `open`/`read`/`write`/`seek`/`close`, `rename`, `listdir`, `makedirs`, `exists`, `remove`, permission changes, symlinks, xattrs, plus JuiceFS extensions (`warmup`, `summary`, `rmr`, `info`).
A native fsspec filesystem ships in the same package (import path `sdk.python.juicefs.juicefs.spec`, used for Ray/AI dataloaders).
Both are beta in 1.3/5.1.
Because it is in-process and fsspec-compatible, JuiceFS drops straight into the same ~50-line function-injection bridge that backs Monty / Starlark / PyMiniRacer against any fsspec filesystem, with no FUSE, no mount lifecycle, and no kernel dependency.

**FUSE and other access modes (for contrast).**
The traditional `juicefs mount` path runs a long-lived client process holding the FUSE mount: it must stay resident (death means a stale mount), it holds credentials to both backends, it maintains a local cache directory, and each host needs its own mount.
FUSE also caps reads at 128 KB, fragmenting large reads; this was one motivation for the SDK.
The FUSE-free alternatives besides the SDK are the S3 Gateway (`juicefs gateway`, a MinIO interface), the WebDAV gateway, and the Java/Hadoop SDK (same `libjfs`).
The Kubernetes CSI driver does _not_ avoid FUSE, since it runs a mount pod.

**What it gives natively vs. what an app still builds.**
This is the load-bearing comparison for a system building its own per-file versioning and content-addressed dedup on S3.
Content-addressed dedup: no. Blocks are named by sequential ID rather than content hash, so two identical files written independently produce separate, duplicate blocks — the direct contrast with a BLAKE3 content-dedup design.
Per-file version history: no. Blocks are immutable and edits append new slices, but there is no queryable per-file version lineage; superseded slices become garbage to compact or trash, not retained versions.
Snapshots and clones: yes, via `juicefs clone`, a metadata-only copy-on-write that shares the original's blocks until modified.
This is the only block-sharing mechanism, and it is explicit, never automatic.
Trash: yes, on by default.
Deletes and stale slices move to a hidden `.trash/` retained for `--trash-days`, recoverable via `mv` or `juicefs restore` (v1.1+).

**Multi-tenancy.**
A JuiceFS volume is a single flat POSIX namespace with no native per-tenant workspace abstraction.
Isolation primitives exist: POSIX permissions and ACLs (1.2+), `squash` UID remapping (Community), access tokens scoped by subdirectory or IP (Cloud/Enterprise only, gating metadata rather than object-store credentials), Kerberos/Ranger (Enterprise), and S3-Gateway IAM (enhanced gateway).
But JuiceFS's own recommendation for _strong_ tenant isolation is a separate filesystem per tenant, which scales poorly to thousands of users.
For thousands of isolated workspaces on one volume, the application still enforces isolation via path-prefixing and its own authz, exactly as it would on raw S3.

#### filestash (mickael-kerjean/filestash)

Filestash is a self-hosted file-management platform and universal data-access layer with 13.9k GitHub stars, built with a Go backend and a vanilla-JavaScript frontend.
Everything in it is a plugin: storage backends, auth, authorization, viewers, search, thumbnailing, frontend patches, middleware, endpoints.
The core `IBackend` interface is deliberately minimal — `Ls`, `Stat`, `Cat`, `Mkdir`, `Rm`, `Mv`, `Save`, `Touch` — so any storage implementing these 8–10 methods becomes a first-class backend.
It supports over 20 storage protocols (FTP, SFTP, S3, SMB, WebDAV, IPFS, NFS, virtual filesystems) and reaches more than 200 file formats (photography, science, GIS, data engineering, biomedical, 3D, and more).
Access gateways include a web client, an SFTP gateway, an **MCP gateway**, and an S3 gateway, plus a workflow engine that chains actions on file events for notifications and MFT pipelines.
It has no FUSE dependency, and its RBAC authorization supports pluggable authentication, including delegation to external systems like WordPress.

filestash is relevant as a reference for a universal data-access layer: its 8-method `IBackend` interface proves a simple contract can unify wildly different storage systems, its plugin architecture keeps the core minimal, and its MCP gateway already exposes a unified filesystem to agents via the Model Context Protocol.

#### AGFS / Aggregated File System (c4pt0r/agfs)

AGFS unifies backend services — KV stores, message queues, databases, object storage — as filesystem operations.
It has 331 stars, is written in Go, C++, Python, and Rust, and describes itself as a tribute to Plan 9.
`redis.set("key", "val")` becomes `echo "val" > /kvfs/keys/mykey`; `sqs.send_message(queue, msg)` becomes `echo "msg" > /queuefs/q/enqueue`.
Multiple virtual filesystems compose in one namespace: `kvfs/`, `queuefs/`, `s3fs/`, `sqlfs/`, `heartbeatfs/`, `memfs/`.
SQL access runs through a Plan 9-style session interface (create a session via `cat ctl`, write a query, read the result).
Agents register by `mkdir` and heartbeat by `touch keepalive`, with auto-removal on timeout.
Cross-FS operations like `cp local:/tmp/data.txt /s3fs/mybucket/` work directly, and a custom shell (`agfs-shell`) supports scripting via `.as` files.
It is reachable over FUSE on Linux or an HTTP API where FUSE is unavailable.

AGFS is the most philosophically aligned project in the survey: agents understand file operations natively, so the design bet is that all services should be exposed as files, composed into one unified namespace.
Its main gap for ai-vfs is that it is a server-side system with its own shell, not an embeddable library.

#### TigerFS (timescale/tigerfs)

TigerFS is a filesystem backed by PostgreSQL that exposes database tables and rows as files and directories.
It is written in Go, has 200+ stars, and mounts via FUSE on Linux or NFS on macOS.
It comes from Tiger Data, the company formerly known as Timescale, creators of TimescaleDB.
It runs in two modes: file-first, where markdown files are stored as Postgres rows with frontmatter as columns, and data-first, where an existing Postgres database is mounted and explored with `ls`/`cat`/`grep`.
Architecturally, Unix tools reach PostgreSQL through FUSE or NFS and a TigerFS daemon; every file is a real PostgreSQL row, every directory is a table, and file contents are columns.
Pipeline query paths push down to SQL: `.by/customer_id/123/.order/created_at/.last/10/.export/json` resolves as one optimized query.
Every operation is an ACID transaction, so `mv` is atomic and multiple agents can read and write concurrently without coordination.
Version history comes automatically from TimescaleDB hypertables, with every edit stored as a timestamped snapshot under `.history/`.
An apps system lets a table become a directory of `.md` files with YAML frontmatter by writing `"markdown,history"` to `.build/`.
Multi-tenant agent-coordination patterns include task queues moved between `todo`/`doing`/`done` directories via `mv`, and shared knowledge bases with immediate visibility.

What TigerFS is not is the limit that recurs in every fit verdict for it: no Python API, no PyPI package, no in-process library mode, and no public GitHub repository (the linked `github.com/timescale/tigerfs` appears private).
The entire API surface is the filesystem itself; interaction happens through `ls`, `cat`, and `echo >` on a mount point, and the project appears to be in early access or beta.
It has no direct S3 or cloud object-storage support, since the backend is exclusively PostgreSQL, and its FUSE requirement makes it unsuitable for containerized environments without privileged access.
Multi-tenant isolation is only standard Postgres permissions.
For sandbox integration its sole mechanism is mount-then-point: mount via FUSE, then aim the sandbox's filesystem layer at the mount (for WASI, `preopen_dir("/mnt/tigerfs", "/data")`).
**Disambiguation:** TigerFS (Tiger Data / Timescale) is entirely unrelated to TigrisFS (Tigris Data), an S3-compatible FUSE adapter, and to ZeroFS (see Part 3.3).

TigerFS is the survey's strongest demonstration that "the filesystem is the API" for agents.
Its patterns are worth adopting even where the product is not: a database-backed filesystem for ACID and concurrent multi-agent access, version history per file, pipeline query paths, and task-queue-as-directories.
Its main limitation for ai-vfs is tight coupling to PostgreSQL, compounded by the FUSE-only, API-less surface.

#### Rust storage abstractions: object_store and Apache OpenDAL

`object_store` and Apache OpenDAL are not filesystems themselves but the cloud-storage abstraction layer a Rust-side sandbox trait implements against.
`object_store`, from Apache Arrow, supports S3, Azure Blob, and GCS; its trait methods map cleanly onto filesystem operations (`read_file` becomes `get().bytes()`, `write_file` becomes `put()`, `read_dir` becomes `list_with_delimiter()`).
Apache OpenDAL covers more than 40 storage backends.
Both are actively maintained and align with tokio-based async sandboxes.

### 1.2 Virtual bash interpreters (in-process, no OS shell)

#### just-bash (vercel-labs/just-bash)

just-bash is a virtual bash environment with an in-memory filesystem, written in TypeScript and designed for AI agents; it has 2.3k GitHub stars and comes from Vercel Labs (contributors include Malte Ubl).
All 75+ of its commands are reimplemented in TypeScript, so it supports standard Unix commands and bash syntax broadly without spawning real processes: no `fork`/`exec`, no real shell.

Its filesystem layer offers four backends behind one `IFileSystem` interface: `InMemoryFs` (the default), `OverlayFs` (copy-on-write over a real directory), `ReadWriteFs` (direct disk access), and `MountableFs`, which composes multiple filesystems at different mount points.
`MountableFs` supports dynamic `.mount()` calls — a cloud-backed filesystem at `/cloud`, say, alongside `InMemoryFs` at `/tmp`.
Each `exec()` call gets isolated shell state (environment variables, working directory reset); the filesystem itself stays shared across calls.
Network access is disabled by default, and when enabled it is enforced through URL-prefix allowlists with header transforms for credential injection.
Optional Python (CPython-to-WASM) and JavaScript (QuickJS-to-WASM) execution ships off by default; the Pyodide path carries the CVE-2025-68668 caveat discussed in §1.3.
AST-transform plugins instrument bash scripts before execution, `defineCommand` adds domain-specific operations, and configurable limits cover max call depth, command count, and loop iterations.

just-bash ships as a library (`just-bash`), an AI SDK tool (`bash-tool`), and a `Sandbox` class API-compatible with `@vercel/sandbox`.
It runs in both Node.js and the browser.
Cloud-backing means implementing `IFileSystem` in TypeScript against `@aws-sdk/client-s3` or similar.
The simplest path today is staging: download files as a `Record<string, string>`, pass them as `new Bash({ files })`, execute, and upload results.
The limitation to plan around: `InMemoryFs` is bounded by the Node.js heap (~1.5 GB by default), `OverlayFs` and `ReadWriteFs` cap reads at 10 MB by default, and large datasets need a streaming path that doesn't yet exist.

just-bash is a direct precedent for giving agents a sandboxed filesystem.
The `MountableFs` pattern and the `OverlayFs` behavior (reads pass through to disk, writes stay in memory) apply directly to agent workspaces that need to see real project files while containing their mutations.

#### bashkit (everruns/bashkit)

bashkit is a virtual bash interpreter with a virtual filesystem, written in Rust for multi-tenant environments — conceptually similar to just-bash but with PyO3/NAPI-RS bindings.
It has 100+ GitHub stars (30 at the time of the earliest research turn) and ships as `bashkit` v0.1.4 on crates.io.
All 150 of its commands are reimplemented in Rust: zero process spawning, fully in-process.
It is POSIX-compliant (IEEE 1003.1-2024 Shell Command Language) with extensive bash extensions: arrays, associative arrays, brace expansion, extended globs, process substitution, coprocesses.

Its VFS layering matches just-bash's: `InMemoryFs`, `OverlayFs`, `MountableFs`, plus an optional `RealFs` backend, all behind a `FileSystem` trait used as `Arc<dyn FileSystem>`, with `MountableFs` mounting different backends at different paths.
Security is defense-in-depth, with 60+ identified threats across 11 categories, each carrying a mitigation and test coverage.
On top of that sit parser limits (timeout, fuel budget, AST depth), filesystem limits (total bytes, file count), panic recovery via `catch_unwind`, resource limits on command count, loop iterations, and function depth, and a network allowlist scoped per-domain over HTTP.

bashkit is built to be called by an LLM directly.
Its `BashTool` contract carries streaming output, system prompts, and discovery metadata, with first-class pydantic-ai integration via `bashkit.pydantic_ai.create_bash_tool()` and LangChain integration through the Python bindings.
Scripted tool orchestration composes multiple tool definitions into multi-tool bash scripts, and an MCP server mode runs via `bashkit mcp`.
Experimental features include virtual git operations on the VFS and embedded language runtimes: Python via Monty, TypeScript via ZapCode. bashkit and Monty are designed to compose.
Language bindings cover Python (PyO3, `bashkit-python` on PyPI) and JavaScript/TypeScript (NAPI-RS, for Node, Bun, and Deno).
Each `Bash` instance is fully isolated, which is what makes it multi-tenant.

Cloud-backing means implementing the `FileSystem` trait in Rust, most naturally against the `object_store` crate or Apache OpenDAL.
`MountableFs` accepts any `Arc<dyn FileSystem>`, so a custom Rust backend wrapping `object_store` bridges S3, Azure, and GCS — roughly 500 lines of Rust, estimated at one to two weeks of work — and bashkit's tokio-based async architecture aligns with these async storage crates.
A Python-side staging pattern works today; live cloud access requires Rust, because the Python bindings do not expose the filesystem trait to Python.

Of everything surveyed, bashkit is the most complete vision of a sandboxed virtual environment for agents.
VFS, bash interpreter, and embedded language runtimes (Monty for Python, ZapCode for TypeScript) together amount to a virtual OS layer, with better performance and memory safety than just-bash's TypeScript equivalent.
Files written by bash are readable from embedded Python and vice versa.

### 1.3 In-process Python execution

#### Pydantic Monty (pydantic/monty)

Pydantic Monty is a minimal, secure Python interpreter written from scratch in Rust for running LLM-generated code, with 6.4k–6.6k GitHub stars.
It uses Ruff's parser for Python source, compiles to its own bytecode format, and executes in a custom VM — not CPython with restrictions, and not Python compiled to WASM.
Access is zero by default: no filesystem, no network, no environment variables, and all external interaction happens through explicitly provided **external functions**.

Because it is embedded in the host process, Monty starts in sub-microsecond time: about 0.06 ms, or 3–15 µs cold in later measurements, roughly 10,000× faster than a subprocess.
For reference, CPython's `exec()` takes ~0.1 ms, Docker ~195 ms, and cloud sandbox services 1000 ms or more.
Its **snapshotting** capability serializes execution state to bytes mid-flight, at external function-call boundaries, for storage and later resumption in a different process.
Snapshots run single-digit kilobytes; a VM snapshot runs gigabytes.
This works through an **iterative execution model**: `start()` runs until an external function call and returns a `FunctionSnapshot`, the host resolves the call, and `resume()` continues.
That enables pause/resume workflows such as waiting on human approval, plus non-blocking, event-driven execution across many tenants at once.

Monty type-checks via a bundled `ty` in a single binary.
It is callable from Rust, Python, or JavaScript, with resource limits on memory usage, allocations, stack depth, and execution time.
Its stdlib is limited to `sys`, `os`, `typing`, `asyncio`, `re`, `datetime`, and `json` — roughly 50% stdlib coverage.
By design it has no classes yet, no `match`, no `with`/context managers, no generators, and no third-party packages. (Versions referenced here: v0.0.8, which the limits above describe, and v0.0.18, which adds async `run_async` and an `os=` argument that mounts a native filesystem.) Bytes are supported natively, but Monty cannot pass file-like objects across the sandbox boundary, so reads and writes are whole-file operations (`cat`/`pipe`), not streaming.
It is already integrated into bashkit as the embedded Python runtime.
Pydantic AI's `CodeModeToolset` demonstrates the pattern more broadly: the LLM writes Python that calls tools as functions, executed safely inside Monty.

Monty is the execution-engine complement to a virtual filesystem.
Expose `read_file` / `write_file` / `list_dir` as external functions, and the LLM's Python interacts with the VFS with no real filesystem access; snapshotting persists agent execution state alongside filesystem state.
It is best used as a fast path (the tiered model in Part 2), not a security boundary for adversarial code — it shares a heap with the host process and has no hardware memory isolation.

#### Eryx (eryx-org/eryx)

Eryx is a Rust library, Apache-2.0/MIT and by Ben Sully, that executes Python in a WebAssembly sandbox via Wasmtime with async callbacks.
It launched in early 2026 directly targeting multi-tenant LLM code execution, has 41–45 GitHub stars, and ships as a Rust crate, a Python package (`pyeryx` on PyPI), and an npm package.
Unlike Monty's reimplemented interpreter, it runs real CPython 3.14 compiled to WASI, using the Bytecode Alliance's componentize-py.
It bridges the sandbox through an async callback mechanism: host Rust functions are exposed as `await`-able Python functions inside the sandboxed code.

Startup is engineered aggressively.
Pre-initialization captures Python's initialized memory state at build time for a ~25× speedup in creation (450 ms → 18 ms); pre-compiled Wasm (AOT) gets a ~41× speedup (650 ms → 16 ms); per-execution overhead drops to about 1.6 ms once pre-compiled.
A `SandboxPool` keeps warm instances with pre-warming, bounded concurrency, idle eviction, and statistics tracking.
Session state (variables, functions, classes) persists between executions REPL-style, with snapshots via pickle serialization, execution tracing via `sys.settrace`, and cancellation via Wasmtime epoch interruption.
Networking is host-controlled TCP/TLS with per-host allowlists, disabled by default.
Eryx also supports virtual filesystem mounting, secret scrubbing, an MCP server, composable runtime libraries with type stubs, and experimental native-extension support (numpy compiled to WASI can work via late-linking).

The project is young — 413 commits, 4 contributors, including AI assistance — but architecturally it is the most complete solution surveyed for this specific problem.
Eryx sits at a different point on the sandbox spectrum than Monty: full CPython compatibility (classes, stdlib, some packages) at the cost of higher startup latency (~16 ms pre-compiled vs. Monty's ~0.06 ms).
It matters when agents need third-party packages or full Python semantics.
Its callback mechanism, like Monty's external functions, bridges VFS operations into the sandbox.

#### wasmtime-py + CPython.wasm

wasmtime-py is the raw WASM runtime beneath Eryx: v41.0.0 (January 2026), maintained monthly by the Bytecode Alliance, and the clear winner among WASM runtimes with Python bindings.
It offers fuel-based CPU metering (`store.add_fuel(400_000_000)`), epoch-based interruption for deadline timeouts, memory limits via `store.set_limits()`, and WASI preopen directories for capability-based filesystem control.
Each `Store` object is a strict isolation boundary; WASM objects from different stores cannot interact.
The pattern is therefore to share one compiled `Module` (CPython.wasm) across all tenants and create a fresh `Store` + `WasiConfig` per execution.

The proven version of this pattern, documented by Simon Willison, loads VMware's pre-built `python-3.11.1.wasm` with zero preopened directories, captures stdout/stderr to files, and sets a fuel budget that traps on exhaustion.
Cold start takes about 650 ms to compile CPython on first load, reducible to about 16 ms with AOT compilation.
The full CPython stdlib is available and all six security vectors (§2.1) are blocked, but C-extension packages are not.

The binding-level constraint is `WasiConfig.preopen_dir(host_path, guest_path)`: it accepts only real OS filesystem paths.
There is no Python-level mechanism to substitute a virtual or in-memory filesystem, because the binding is a thin ctypes wrapper over the C API and the lower-level Rust `WasiDir`/`WasiFile` traits are not exposed to Python.

#### Pyodide (+ Deno on the server)

Pyodide is CPython ported to WebAssembly via Emscripten; it requires a JavaScript runtime (V8 or SpiderMonkey) and cannot run inside wasmtime.
Its advantage is ecosystem breadth: full CPython 3.13 with NumPy, Pandas, scikit-learn, and many other C-extension packages pre-ported.

On the server, Pyodide alone provides no security boundary.
CVE-2025-68668 (CVSS 9.9, Cyera Labs) proved this: an escape via `_pyodide._base.eval_code()` plus ctypes-like indirection invokes `system()` without ever touching `os.system`.
Grist-Core suffered a similar escape (CVSS 9.1), fixed by moving Pyodide into Deno.
The resulting safe server pattern is Pyodide + Deno: Deno's `--deny-read --deny-write --deny-net --deny-env` flags restrict the process, and Pyodide's Emscripten MEMFS supplies an ephemeral in-memory filesystem.
LangChain's `langchain-sandbox`, Pydantic's `mcp-run-python`, HuggingFace smolagents' `WasmExecutor` (v1.20.0+), and Cloudflare Workers (Pyodide in V8 isolates for Python Workers) all use this pattern.
Its downside is that it spawns a Deno subprocess per execution — not truly in-process — and it lacks wasmtime's fuel metering, relying instead on timeout-based process killing.

In the browser, the calculus inverts entirely; see Part 2.3.

#### Other Python-in-WASM options

Three more Python-in-WASM options were ruled out.
RustPython compiles to WASM/WASI (`cargo build --target wasm32-wasip1`) and runs in wasmtime with full capability isolation, but its Python compatibility is incomplete — by its own description, "in development, not totally production-ready."
MicroPython compiled to WASM is too limited, missing most of the stdlib, for general LLM-generated code. componentize-py, from the Bytecode Alliance, compiles Python into WASM Component Model components; it is a build-time tool, not a runtime sandbox, though Eryx uses its CPython WASI build internally.

### 1.4 JavaScript engines from Python

#### PyMiniRacer (bpcreech/PyMiniRacer, PyPI `mini-racer`)

PyMiniRacer embeds V8 (14.4) in Python, one isolate per `MiniRacer()` instance, with complete memory isolation between instances, the V8 sandbox enabled on all platforms, and hard heap memory limits and eval timeouts built in.
Its API is `ctx.eval("code")`, `ctx.call("fn", args)`, and `wrap_py_function()` for callbacks; each isolate starts at ~1.7 MB of physical memory against a ~37 MB shared library, so hundreds of isolates per process are practical.
It originated at Sqreen in 2016 and has been revived and actively maintained by Ben Creech since March 2024, with pre-built wheels for all major platforms.

`wrap_py_function()` injects Python functions as async JavaScript functions returning Promises — V8's single-threaded architecture would deadlock on synchronous callbacks, so every injected filesystem function must be awaited on the JS side, which aligns naturally with fsspec's `AsyncFileSystem` implementations.
Its standout advantage is native bytes-to-ArrayBuffer conversion: PyMiniRacer is the only JS sandbox surveyed that handles binary files without base64.
There is no filesystem or network access by default, since V8 is a pure computation engine, and the main limitation is that it is JavaScript only — no TypeScript compilation, no Node.js APIs.

#### QuickJS (PetterS/quickjs, PyPI `quickjs`)

QuickJS, wrapping Fabrice Bellard's engine, is a lighter alternative: the entire runtime is ~600 KB, with built-in `set_memory_limit(bytes)` and `set_time_limit(seconds)`, zero host access unless explicitly injected, a thread-safe `Function` class, and multiple isolated `Context` instances per process.
Its callback model is simpler than PyMiniRacer's — synchronous `add_callable(name, func)` rather than async.

The `quickjs` package itself was archived in January 2026 and is no longer maintained.
It supports only string and numeric types (no bytes), and its thread-hostile `Context` class complicates multi-tenant deployments.
QuickJS-NG (quickjs-ng/quickjs, 2,800 stars) is the actively maintained community fork, with 16 releases through February 2026 tracking the latest ECMAScript spec.
Rust bindings exist via `quickjs-rusty`, but a dedicated Python binding for QuickJS-NG is emerging and not yet mature on PyPI.
For new projects, PyMiniRacer is the stronger choice despite its larger footprint (~37 MB vs. ~2 MB).

#### Other JS engines (no usable Python binding)

A handful of other JS engines have no usable Python binding today.
`deno_core`, the Rust crate powering Deno, can be embedded via PyO3 — VlConvert has done this — but no pre-built Python package exists.
STPyV8 offers deep Python-JavaScript interop, but its large attack surface makes it unsuitable for sandboxing.
Boa (a Rust JS engine) and Cloudflare workerd (a standalone binary, not an embeddable library) have no Python bindings at all; the closest thing to self-hosted Deno Subhosting is Supabase Edge Runtime, a Docker container with dual-runtime isolation — a trusted main runtime alongside a restricted user runtime.

### 1.5 Constrained expression / scripting languages

#### Starlark (starlark-pyo3)

Starlark-pyo3 provides Python bindings, via PyO3, for starlark-rust — Google's Python-like configuration language for Bazel.
It has only 36–41 GitHub stars but roughly 2.4M monthly PyPI downloads.
Its restrictions are deliberate and make it the strongest isolation guarantee of any option surveyed, hermetic by language design: evaluation is deterministic; execution has no filesystem, network, or system clock access; shared data becomes immutable, so it is parallel-safe; there are no `while` loops (which structurally prevents infinite loops), no recursion beyond a configurable limit, no classes, and no exceptions.
Startup is very fast (~1.7 ms) and fully in-process.
It exposes `Module` and `Globals` for evaluation, injects host functions via `module.add_callable(name, callable)`, and supports the `Decimal` type and positional-only arguments.

The integration limitation is that all values pass through JSON serialization: no bytes type, so binary must be base64-encoded, and large payloads incur overhead.
There is also no exception handling at all — filesystem errors terminate evaluation, so error-resilient code has to check `file_exists()` before reading.
LLMs generate Starlark easily, since it reads syntactically like Python.
That makes it well suited to data transformation, policy evaluation, and configuration, though not to general-purpose code given the absence of classes, OOP, and much of the stdlib.
Monty has largely superseded it for the "safe LLM code execution" use case, but Starlark remains the reference for a deliberately restricted, deterministic, reproducible environment.

#### CEL (Common Expression Language)

CEL is inherently safe: non-Turing-complete, side-effect free, with guaranteed termination.
The `common-expression-language` package (v0.5.6, February 2026) wraps a Rust core via PyO3 with microsecond evaluation, and Google announced `cel-expr-python` (March 2026), wrapping the official C++ implementation.
It excels at policy evaluation, data filtering, and validation rules, but it cannot express loops or function definitions.

#### Lua via lupa (PyPI `lupa` v2.6)

lupa embeds Lua 5.4 or LuaJIT in Python, with separate `LuaRuntime` instances per tenant at roughly 600–800 KB each.
Isolation requires manual environment whitelisting: remove `io`, `os`, `debug`, and `package`, and provide only safe functions.
Lua 5.4's `debug.sethook` enables instruction-count limits for infinite-loop prevention, but LuaJIT does not support this, so sandboxing needs PUC-Rio Lua 5.4 specifically.
The `sandbox.lua` library (kikito/lua-sandbox) provides a ready-made whitelist with a 500,000-instruction default quota.
Two related projects don't fit.
Mozilla's `lua_sandbox` is a C library for telemetry and data pipelines with no Python bindings and little recent activity.
Luau, Roblox's Lua 5.1 derivative, has built-in sandboxing used for millions of untrusted scripts but lacks Python bindings entirely.

#### Other constrained languages

A few other constrained languages were considered and set aside.
Jsonnet is Turing-complete but output-only (JSON), and its `import` mechanism reads arbitrary files unless restricted through a custom `import_callback`.
Rhai (Rust, ~5k stars) has an excellent sandbox design with configurable memory and CPU limits but no Python bindings.
Tcl's safe interpreter (`interp create -safe`) is a historically interesting dual-interpreter model, accessible via `tkinter`, but it is awkward to integrate, LLMs don't generate Tcl, and it has no resource limits.
Boa, Tengo (Go), and mRuby round out the list — all lack Python bindings.

### 1.6 WASI virtual-filesystem implementations

For WASI-based sandboxes, whether a _virtual_ (non-real-OS) filesystem can back the guest depends on the implementation.
The canonical extensibility point is wasmtime's `wasi-common` `WasiDir`/`WasiFile` traits; the crate provides no filesystem of its own (only `ReadPipe`/`WritePipe` for virtual streams), so embedders supply their own via `wasi-cap-std-sync` (real OS) or custom implementations.
Wasmer's `virtual-fs` is an excellent reference architecture (in-memory FS, overlay FS, chained FS) but is not compatible with wasmtime.
An open wasmtime issue (#8963) confirms the newer WASIp2 path lacks clear documentation for custom filesystem providers, despite the legacy `wasi-common` path being well-designed for it.

| Implementation                   | Runtime               | Read/Write | Dynamic backend             | Python-accessible | Maturity                                   |
| -------------------------------- | --------------------- | ---------- | --------------------------- | ----------------- | ------------------------------------------ |
| **wasi-common WasiDir/WasiFile** | Wasmtime              | Read-write | Yes (custom Rust impl)      | Rust only         | Mature, designed for this                  |
| **wasi-vfs**                     | Any WASI runtime      | Read-only  | No (build-time embed)       | CLI tool          | Stable (v0.5.5)                            |
| **WASI-Virt**                    | Wasmtime (components) | Read-only  | No (post-compile)           | CLI tool          | Early (154 stars)                          |
| **Wasmer virtual-fs**            | Wasmer only           | Read-write | Yes (custom Rust impl)      | No                | Active (v0.601.0)                          |
| **cap-std**                      | Wasmtime (underlying) | Read-write | No (real OS only)           | No                | Mature                                     |
| **tmpdir staging**               | Any                   | Read-write | Yes (via fsspec)            | Python            | Production-ready                           |
| **FUSE mount**                   | Any                   | Read-write | Yes (s3fs-fuse/fsspec.fuse) | Python            | s3fs-fuse mature; fsspec.fuse experimental |

### 1.7 Adjacent projects and cloud sandbox providers

**Closest existing integrations of a virtual FS with a sandbox:**

- **AgentFS** (Turso/tursodatabase) is a SQLite-backed virtual filesystem purpose-built for AI agents, storing files, key-value pairs, and audit trails in a single `.db` file; it supports FUSE mounting, copy-on-write overlays via Linux mount namespaces, and runs in browsers via WASM.
  It is the most directly relevant architectural pattern surveyed — virtual FS, then sandbox, then code execution — though it uses its own SDK rather than fsspec.
- **Localsandbox** (CoPlane) combines AgentFS, just-bash, and Pyodide into one local agent sandboxing solution — the closest existing project to the desired architecture: a SQLite virtual filesystem connected to both a bash interpreter (just-bash) and a Python WASM sandbox (Pyodide), where files written inside either sandbox go to SQLite rather than the real filesystem.
- **llm-wasm-sandbox** (PyPI) is a production-grade WASM sandbox for executing untrusted Python or JavaScript from LLMs, with a pluggable storage-adapter interface and UUID-based per-session workspace isolation — it mirrors the fsspec-integration pattern this research is aiming for.

**Cloud sandbox providers** (OS-level, not in-process — out of scope but the baseline being avoided):

- **E2B** (e2b.dev) runs Firecracker microVMs with ~150 ms creation time; per its own marketing it is used by 88% of the Fortune 100.
  It is integrated into HuggingFace smolagents, LangChain, and Manus, and supports full Linux, any language or library, and roughly 50,000 concurrent sessions.
- **Modal** runs container-based sandboxes with gVisor, sophisticated filesystem snapshots, pay-per-CPU-cycle pricing, and sub-second spinup.
- **Daytona** runs Docker/OCI containers and raised a $24M Series A.
- **Fly.io Sprites** are persistent Firecracker VMs with 100 GB of NVMe plus object storage.
- **llm-sandbox** (vndee/llm-sandbox) wraps Docker, Podman, and Kubernetes.

Three more runtimes were noted and ruled out.
Extism is a higher-level WASM plugin abstraction over Wasmtime with a clean Python SDK, per-plugin memory limits, and a host-function decorator, but it has no direct fuel metering and requires plugins to conform to its ABI.
WAMR remains experimental, with undocumented Python bindings not published to PyPI. wasmer-py is dead — last released in January 2022, incompatible with Python 3.11+ — because Wasmer pivoted to cloud and edge.

---

## Part 2 — Analysis threads

Cross-cutting findings that reference the tools above rather than re-describing them.

### 2.1 The security argument: WASM is the only in-process boundary that holds

#### The constraint: start from nothing, not a restricted real runtime

The defining constraint is "start from nothing, allowlist capabilities," not "start from a real runtime, restrict it."
Pydantic Monty and Bashkit embody the former.
OS-level approaches such as nsjail and bubblewrap embody the latter, and the multi-tenant-web-app constraint (Part 0) rules them out.

#### WASM blocks all six security vectors; nothing else in-process does

The Arize Phoenix team (February 2026, `github.com/Arize-ai/phoenix` issue #11756) empirically tested six attack vectors across every major sandbox backend: memory isolation, env-var exfiltration, outbound networking, filesystem reads, subprocess spawning, and CPU/memory exhaustion.
CPython compiled to WASM running inside wasmtime was the sole no-sidecar option passing all six.
The advantage is hardware-enforced linear memory isolation (each WASM instance's memory physically cannot address host memory) combined with WASI's capability model (zero filesystem or network access unless explicitly granted) — a sandbox that depends on neither blocklists, AST rewriting, nor environment stripping.
An 8-thread wasmtime pool achieves 130–198 executions per second at ~30 ms p50 latency.

#### Python-level sandboxing is fundamentally broken

Every CPython core developer who has commented on this — Victor Stinner (pysandbox's author), Alyssa Coghlan, Brett Cannon — agrees: "run the Python process in a sandbox, don't run a sandbox in Python."
The language's introspection makes escape from any Python-level sandbox structurally inevitable.
The record backs the claim.
PEP 578 audit hooks (`sys.addaudithook()`) are explicitly not a sandbox; the PEP says so.
They are advisory only: ctypes and C-extension access bypass them, and `__subclasses__()` chains reach dangerous classes without ever triggering an audited operation.
That leaves them useful only as a monitoring or logging layer.
Python subinterpreters (PEP 684/734, Python 3.12–3.14) provide per-interpreter GILs for concurrency but zero security isolation, which is why Brett Cannon puts it plainly: "if you want Python in a sandbox you're probably best using the WASI build of CPython."
Import restrictions via `sys.meta_path` are trivially bypassed through `__subclasses__()` chains, `importlib.import_module()`, or simply traversing objects already in memory.
RestrictedPython (Zope/Plone) says as much in its own docs: it "is not a sandbox system or a secured environment."
PyO3 embeds the full CPython interpreter (discussion #2080: "pyo3 is not meant for sandboxing").
PyPy Sandbox had an excellent design, marshaling all syscalls through stdout to an external controller, but it is effectively unmaintained and has been removed from PyPy mainline.
The one novel Python-level approach worth noting is sandboxed-python (`pip install sandboxed-python`), which implements "Finite Python" (FPy): a restricted subset with no loops and no recursion, enforced through AST analysis and allowlists, designed for LLM tool calls.
It is too restrictive for general code, but interesting as a fast pre-screening layer.

#### V8 isolates alone are not sufficient

V8 has had numerous sandbox-escape CVEs.
Cloudflare patches within hours of V8 security releases and layers process isolation, kernel features, and rapid patching on top of workerd's V8 isolates.
The open-source workerd README says as much directly: it "does NOT contain suitable defense-in-depth against implementation bugs."
Treat PyMiniRacer, QuickJS, and Monty alike as a fast path, not a security boundary for adversarial code.

#### The codemode / CodeAct interaction pattern

The LLM tool-call loop runs like this: the LLM emits code, a router dispatches it to Monty for Python or Bashkit for Bash, the result returns as a tool result, and the LLM continues.
The CodeAct pattern (ICML 2024) showed 20% higher success rates and 30% fewer interaction steps than JSON tool calls; Cloudflare's Code Mode and Pydantic AI's `CodeModeToolset` are the same idea.
This is why the fast in-process interpreters matter: most agent work is orchestration code, not adversarial computation.

#### Three integration architectures, one that fits

Three integration architectures were weighed for the platform.
Lightweight Linux namespace isolation (bwrap + seccomp + Landlock) violates the no-OS-primitives constraint.
An in-process embedded interpreter — Pydantic Monty plus fsspec external functions — fits.
A hybrid, Monty for fast tool calls plus bwrap for full execution, still partially violates the constraint.

#### The tiered execution model

The synthesized recommendation selects an execution tier per request, based on code-complexity analysis:

```text
Tier 1 — Expression evaluation (<1 ms)
  → Starlark (starlark-pyo3) or CEL
  → For: simple expressions, data filtering, policy rules
  → Guaranteed termination, zero I/O, hermetic by design
  → Route here when AST analysis shows no imports/loops/function defs

Tier 2 — In-process interpreted execution (µs–ms)
  → Pydantic Monty (Python subset) or QuickJS / PyMiniRacer (JS)
  → For: high-volume LLM tool-call orchestration
  → Fast path, NOT a security boundary for adversarial code (shared heap, no HW isolation)

Tier 3 — WASM-sandboxed full Python (tens of ms)
  → Eryx (preferred, for pooling/pre-init/session state) or wasmtime-py + CPython.wasm
  → Only in-process option blocking all 6 security vectors
  → Fuel metering, WASI preopens, linear memory isolation
  → Accept the stdlib-only limit or invest in Eryx's experimental native extensions

Fallback — Cloud sidecar (hundreds of ms)
  → E2B or self-hosted Firecracker/Docker
  → For: code requiring pip packages (NumPy, Pandas, requests)
  → No in-process solution supports arbitrary pip dependencies securely
```

For JavaScript-only tenants in a Python web app, PyMiniRacer is the simplest path — one V8 isolate per tenant, memory limits, timeouts, no filesystem, pip-installed wheels; for a full JS/TS platform with dynamic code loading, Supabase Edge Runtime is the closest self-hosted Deno Subhosting.

The recommended concrete stack runs both interpreters in one Python process: neither spawns subprocesses, neither touches the host filesystem by default, and per-user isolation is entirely application-layer path namespacing.

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

The stack has honest gaps.
The Bashkit `MountableFs` ↔ cloud-storage integration does not exist yet (an estimated one to two weeks of Rust work against `object_store`); Bashkit itself is new; and Monty's Python subset is orchestration-only (no third-party imports, no classes, no generators), not a full Python REPL.

The landscape matured substantially between 2024 and 2026, for three reasons: CVE-2025-68668 eliminated naive server-side Pyodide as an option; Eryx emerged as a purpose-built CPython-in-WASM sandbox with production-grade pooling; and the Arize Phoenix evaluation supplied the empirical security data validating WASM as the only in-process approach passing every vector.
The durable insight is that isolation has to come from the runtime boundary (WASM linear memory, a V8 isolate), not from language-level restrictions — and C-extension packages still require OS-level isolation via a sidecar.

### 2.2 Three patterns bridge a filesystem into a sandbox

No existing project directly bridges fsspec (or TigerFS) to any of these sandboxes, but every sandbox studied offers a clean extensibility mechanism.
Each sandbox's isolation philosophy dictates how a filesystem layer can reach inside it, and the mechanisms cluster into three patterns.
The most practical near-term approach for every sandbox is a stage-in / execute / stage-out pattern using fsspec; live cloud-backed filesystems require Rust or TypeScript work at the trait level.

#### Pattern 1 — Function injection (Monty, Starlark, PyMiniRacer, QuickJS)

These sandboxes have zero I/O by default and expose a mechanism to register host-side callable functions as globals inside the sandbox: the host wraps fsspec operations as functions and injects them, and sandboxed code calls them by name.
This is pure Python, no Rust, and it works today.
Monty is the cleanest:

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

Monty's **snapshot capability** is particularly valuable for multi-tenant scenarios: a sandbox can pause mid-execution when it needs a file, serialize its state, and resume when the I/O completes — non-blocking, event-driven execution across many tenants.
The limitation is whole-file operations only; no file-like objects cross the boundary.
Starlark injects the same way, via `module.add_callable(name, callable)`, but every value passes through JSON serialization (no bytes; binary must be base64-encoded) and there is no exception handling: errors terminate evaluation, so code has to check `file_exists()` before reading.
That makes it better suited to a configuration or orchestration language than a general file-manipulation environment.
PyMiniRacer's `wrap_py_function()` injects Python functions as async JavaScript Promises, since synchronous callbacks would deadlock V8's single thread, so every injected filesystem function is awaited on the JS side.
Its native bytes-to-ArrayBuffer conversion makes it the only JS sandbox that handles binary files without base64, and it aligns naturally with fsspec's `AsyncFileSystem`.
QuickJS's `add_callable(name, func)` is simpler and synchronous, but the package was archived in January 2026 and handles only strings and numbers.

#### Pattern 2 — Filesystem-trait implementation (Bashkit, just-bash)

These sandboxes route all file operations through a formal filesystem interface — a Rust trait or a TypeScript interface — with built-in in-memory or overlay implementations.
Cloud-backing means implementing that trait against a cloud SDK.
It is the most architecturally elegant pattern surveyed, but it requires writing code in the sandbox's native language.
For Bashkit, that means implementing the `FileSystem` trait (used as `Arc<dyn FileSystem>`) in Rust, backed by `object_store` (S3/Azure/GCS) or OpenDAL (40+ backends).
The method mapping is clean (`read_file` to `object_store::get().bytes()`, `write_file` to `object_store::put()`, `read_dir` to `object_store::list_with_delimiter()`) and aligns with Bashkit's tokio-based async architecture.
A staging pattern works today without any Rust at all: fsspec downloads to a temp directory, Bashkit reads from it, and results sync back.
For just-bash, it means implementing `IFileSystem` in TypeScript against `@aws-sdk/client-s3`, using `MountableFs`'s dynamic `.mount()` to place a cloud-backed filesystem at `/cloud` alongside `InMemoryFs` at `/tmp`.
The simplest path stages files as a `Record<string, string>`, passes them via `new Bash({ files })`, and syncs results back, within `InMemoryFs`'s Node.js heap bound (~1.5 GB) and a 10 MB max-file-read default.

#### Pattern 3 — WASI capability mapping (Eryx, wasmtime-py)

WASM modules under WASI's capability model start with zero filesystem access; the host grants it by "preopening" specific directories and handing over file-descriptor handles.
The fundamental constraint is that `WasiConfig.preopen_dir(host_path, guest_path)` accepts only real OS filesystem paths.
There is no Python-level mechanism to substitute a virtual or in-memory filesystem, since the binding is a thin ctypes wrapper over the C API and the Rust `WasiDir`/`WasiFile` trait system is not exposed to Python.
Three approaches follow.
Staging works today and is Python-only: fsspec downloads to a `tempfile.TemporaryDirectory()`, `preopen_dir(tmpdir, "/data")` points at it, the module runs, and modified files sync back.
It is simple and reliable, but it requires a full download before execution and temp disk space.
A FUSE mount also works today: fsspec's experimental `fsspec.fuse.run()` mounts any fsspec filesystem as FUSE, or the mature `s3fs-fuse` provides an S3 FUSE mount, and `preopen_dir()` points at the mount for transparent lazy-loading.
That path is unavailable in many container environments without privileged access.
The most powerful and most costly approach is a custom Rust `WasiDir`/`WasiFile` implementation. wasmtime's `wasi-common` crate documents this extensibility point explicitly ("this separation of concerns makes it pretty enjoyable to write alternative implementations, e.g. a virtual filesystem").
Implementing `WasiDir` (`readdir`, `open_file`, `create_dir`, and the rest) and `WasiFile` (`read`, `write`, `seek`, `stat`) backed by any data source lets standard Python `open()`/`os.listdir()` inside the sandbox route through transparently.
But it cannot be driven from Python at all, and requires a custom Rust crate or extending wasmtime-py.

Eryx's interaction is callback-based: the host defines typed async callbacks that sandboxed Python calls via `await`.
Two paths exist here.
Eryx-native maps `content = await read_file("/data/file.txt")` on the host side to `fsspec.open("s3://bucket/file.txt")` — clean and explicit, but sandboxed code uses custom functions rather than standard `open()`.
WASI-level uses custom Rust traits and transparent `open()` routing, which is significant Rust work with complex async bridging.

#### The integration roadmap

The immediate, Python-only path that works today is a thin `FsspecBridge` wrapping an fsspec filesystem instance, exposing `read(path) → str`, `write(path, str)`, `list(path) → list`, and `exists(path) → bool` as injectable functions.
That is roughly 50 lines of Python per sandbox, plus path validation, with the staging pattern covering WASI sandboxes.
Because JuiceFS's SDK is in-process and fsspec-compatible, it drops into this bridge unchanged: the cleanest live-cloud-backing option for a function-injection sandbox, and exactly the gap that made TigerFS a blunt instrument.
The medium-term path enables live cloud access through Rust or TypeScript work: Bashkit's `FileSystem` trait backed by `object_store`, just-bash's `IFileSystem` backed by `@aws-sdk/client-s3`, and Eryx's filesystem operations expressed as `TypedCallback` traits bridging to fsspec or `object_store`.
The long-term path is custom `WasiDir`/`WasiFile` Rust traits backed by cloud storage with a caching layer, plus extending wasmtime-py via PyO3 to expose the virtual filesystem to Python.
That would give any WASI-based sandbox a transparent, live cloud-backed filesystem.

The landscape splits cleanly along these lines.
Function-injection sandboxes integrate trivially via Python today; filesystem-trait sandboxes need modest native-language work to implement their trait against cloud SDKs; and WASI sandboxes face a genuine gap, where the Python bindings don't expose the virtual-filesystem extensibility the Rust layer explicitly supports.
The most promising near-term architecture borrows from Localsandbox and AgentFS: use fsspec's `MemoryFileSystem` or `DirFileSystem` as the Python-side abstraction, stage files into whichever sandbox is in use, and sync results back.
For production multi-tenant deployments, Eryx's callback system paired with fsspec's cloud backends offers the best balance of isolation, flexibility, and Python-level control.

### 2.3 The client-side inversion: OPFS + Pyodide dissolves the server-side problem

The prior analysis assumed server-side execution tiers.
Moving the sandbox to the **client** dissolves the server-side isolation problem entirely, resolving all three of the research's core tensions at once: WASI preopened directories require real OS paths, with no Python-level virtual-FS substitution; Pyodide on a server carries CVE-2025-68668 (CVSS 9.9) and is not a security boundary; and the staging pattern forces a full file download before execution.
OPFS (Origin Private File System) plus Pyodide in a browser Worker resolves all three by shifting execution to the client.

**What OPFS provides.**
OPFS is the sandboxed, origin-scoped partition of the File System Access API.
`createSyncAccessHandle()` (Workers only) gives synchronous, byte-level read/write with no permission prompts — exactly what Emscripten's POSIX layer needs to emulate `open()`/`read()`/`write()` inside Pyodide.
Standard Python `open()` therefore works with no staging layer at all.
It persists across sessions: packages write to OPFS on first load and are skipped on subsequent loads, which is how Simon Willison's tools amortize the ~7 MB Pyodide download.
Its isolation is origin-scoped: each browser origin gets a completely separate OPFS partition, so per-user isolation is free, with no application-layer path namespacing required.
Pyodide exposes this through `pyodide.mountOPFS()`, which makes the OPFS partition a standard filesystem path inside the interpreter.

**Reassessing CVE-2025-68668.**
The CVE is a server-side concern: the attack vector is dangerous on a server because the Pyodide process has OS access.
In the browser, Pyodide in a Worker runs in its intended deployment target — the same-origin policy, process isolation, and Worker sandbox are the security boundary, not Pyodide itself — so the "Pyodide + Deno subprocess" workaround becomes unnecessary.

**The architecture inverts.**
Server-side tiers run as:

```text
Server process
    └── in-process sandbox (Monty / Eryx / wasmtime)
         └── staged files ──► fsspec ──► cloud storage
```

A client-side execution tier instead runs as:

```text
Browser Worker
    └── Pyodide (full CPython in WASM)
         └── OPFS (persistent, synchronous) ◄──fetch──► server API ──► cloud storage
```

The server no longer needs an in-process code sandbox at all — it becomes a thin, authenticated cloud-storage proxy.
The browser now provides per-user sandbox isolation; the existing fsspec + `DirFileSystem` layer on the server still provides per-user storage isolation.

**The revised tier model:**

| Tier     | Runtime        | Execution engine                                  | Filesystem                         | Security boundary            |
| -------- | -------------- | ------------------------------------------------- | ---------------------------------- | ---------------------------- |
| 1        | Browser Worker | Starlark / CEL — expression eval                  | N/A (no I/O)                       | Hermetic by design           |
| 2        | Browser Worker | Pyodide + OPFS — full Python, persistent packages | OPFS (sync access handle)          | Browser origin sandbox       |
| Fallback | Server         | Eryx / wasmtime-py                                | Staging or custom Rust WASI traits | WASM linear memory isolation |

Tier 2 replaces both the old server-side Tier 2 (Monty) and Tier 3 (Eryx/wasmtime) for browser-hosted deployments; Eryx and wasmtime-py remain the right choice for headless or server-only execution paths.

**What doesn't move to the client.**
Five constraints keep the server-side layer necessary.
OPFS is browser-only: Node and Deno lack native OPFS, and polyfills exist but are unproven at scale.
`micropip` fetches only pure-Python wheels from CDN, so compiled C extensions must be pre-built as Pyodide wheels.
Browsers allow roughly 60% of available disk per origin as a storage quota, but can evict under pressure, so applications need quota checks and graceful degradation.
Cold-start latency is real: Pyodide is ~7 MB plus packages, and the OPFS persistence pattern is necessary for acceptable UX.
And fetch calls from a Worker are subject to standard CORS, so the server API must set the right headers.

What does not change is that the server-side fsspec + `DirFileSystem` layer remains the correct answer for durable cloud storage (S3, Azure Blob, GCS).
OPFS becomes the local cache and execution layer, and the VFS API endpoints handle sync to and from the cloud — a cleaner separation than the staging-in-tmpdir pattern, because the sync boundary is an explicit API call rather than an implicit temp-directory lifecycle.

---

## Part 3 — Fit against the vision

The verdicts below are anchored to the ai-vfs thesis ([vfs-memo.md](vfs-memo.md)) and the multi-tenant B2C + B2B differentiator (Part 0), **not** to any single, moving spec clause.
Where a verdict depends on an implemented contract, the relevant `specs/` requirement is cited as the _current enforceable expression_ of the thesis — when a spec moves, the citation is refreshed, not the verdict.
The one class of verdict that is genuinely provisional — search — is flagged as such, because that contract is still settling and the thesis alone does not decide it.

### 3.1 Architectural synthesis: what the landscape implies for ai-vfs

The surveyed projects cluster into three layers, and a complete AI virtual filesystem draws from each.
The **filesystem-abstraction layer** contributes fsspec, the Python-native interface standard worth implementing directly for ecosystem compatibility; filestash, whose minimal 8-method backend interface unifies 20+ protocols; and JuiceFS, whose metadata/data separation is the pattern for scalable distributed filesystems.
The **virtual-environment layer** contributes just-bash and bashkit (in-process virtual bash with composable VFS layers: in-memory, overlay, mountable); TigerFS (a database-backed filesystem with ACID transactions and version history); and AGFS (a unified namespace over heterogeneous backends: KV stores, queues, databases, object storage).
The **sandboxed-execution layer** contributes Monty (a from-scratch Python VM in Rust with sub-microsecond startup, snapshotting, and external-function-only I/O); Eryx (full CPython in a Wasm sandbox with async callbacks, sandbox pooling, and native extensions); and Starlark-pyo3 (a niche but deterministic configuration-language evaluator).

A complete ai-vfs would likely combine six of these ideas: an fsspec-compatible interface for Python-ecosystem integration; composable VFS layers (in-memory, overlay, database-backed) from just-bash/bashkit; an AGFS-style unified namespace exposing heterogeneous services as files; TigerFS-style database backing for ACID guarantees and version history; Monty or Eryx as the sandboxed execution engine, with VFS operations exposed as external functions; and a filestash-style plugin architecture for extensible backends.

**How the concrete pieces resolve against the multi-tenant constraint.**
The VFS layer resolves to fsspec + `DirFileSystem`: in-process, per-user isolation via path prefix, no FUSE, with backends for S3, Azure, and GCS.
It is the clear winner; PyFilesystem2 + SubFS is the stalled alternative.
Execution resolves to Monty or Bashkit for Tier 2, Eryx or wasmtime-py for Tier 3, and OPFS + Pyodide for the client tier.
All are in-process or client-side, with no per-user VM or container; per-user isolation is application-layer path namespacing on the server or the browser origin sandbox on the client.
Postgres and MongoDB do not resolve to a filesystem natively: no tool in the survey handles either cleanly as a filesystem, so both are exposed via registered tool functions instead.
TigerFS does not fit as a substrate here.
It is too early-stage and FUSE-dependent, with no Python API, no in-process mode, a PostgreSQL-only backend, and mount-then-point as its only integration mechanism.
Its patterns (files-as-rows, version history, task-queue-as-directories) are worth adopting even though the product itself is not usable as ai-vfs's storage layer. (The composition proofs-of-concept — bashkit+TigerFS via a sync bridge, and Monty+TigerFS via external functions — live in [tigerfs-sandbox-demos.md](tigerfs-sandbox-demos.md).) JuiceFS is a genuine code-reduction option, but only under conditions.
Adopting it would let an application delete its blob-store layer (object-store I/O, chunking, local caching) and the directory-tree mechanics of its metadata store, while talking to it from Python with no FUSE.
It would not remove the two hardest layers, multi-tenant authz and untrusted-code execution isolation, and it would forfeit content-addressed dedup and per-file version history, replacing the latter with explicit `clone` snapshots plus trash.
The added infrastructure is a metadata engine (Redis/SQL/TiKV) alongside the object store, and the Python SDK is currently beta.
JuiceFS is therefore a real code-reduction option only if per-file versioning and content-dedup are not product requirements — orthogonal to, and no substitute for, the sandbox-isolation work.

### 3.2 Storage and search backend fit

> **Purpose.** Research input for a design decision, not a recommendation to adopt or drop any
> provider. Each entry scores one candidate against the contracts the thesis implies for a
> multi-tenant, versioned, permissioned VFS — match, partial, or non-match per role, and whether
> the fit is within the current contract or needs a contract change. The `specs/` requirements
> linked below are the current, enforceable expression of those contracts; verify the specifics
> (transactions, wire version, index behavior, quotas) against current vendor docs before
> building an adapter, as the storage spec already requires for document stores
> ([MetadataTransactions](../../.specs/specs/storage/spec.md#requirement-metadatatransactions)).
>
> **Baseline and provenance.** Catalogued 2026-06-25 against the `specs/` baseline after the
> 2026-06-22 search realignment. The comparison is against the contracts, not against how SQLite
> or Postgres implement them; service capabilities are read from architecture, not a tested
> integration. **The search contract is still settling — treat every search verdict as
> provisional and re-check it when the contract moves;** the thesis alone does not decide whether
> search must be native-in-transaction.

#### Three roles, one contract each

A backend plays up to three roles, each with its own contract: it stores **metadata** (the file tree and its history), stores **blobs** (the file bytes), or serves **search**.
Most candidates fill one or two.
The roles are scored independently because a store can be excellent at one and useless at another.

#### Metadata: the source of truth for files and versions

Metadata holds everything except the bytes: paths, versions, permissions, the audit trail.
Consistency matters more than throughput here, and the contract names three things.
Compare-and-swap on version writes ([MetadataCASSemantics](../../.specs/specs/storage/spec.md#requirement-metadatacassemantics)) is the primitive the system coordinates on, so it sits in the floor: when two writers race to create version 6 of a file, the store admits one and rejects the other by checking "still at version 5?"
in the same step that writes 6.
Literal path-prefix matching ([PrefixQueryLiteralMatching](../../.specs/specs/storage/spec.md#requirement-prefixqueryliteralmatching)) means listing `/my_dir/` matches that exact prefix, and that `_` and `%` are text, not wildcards, so `my_dir` never matches `myXdir`.
Third, the contract must be substitutable across two families, SQL and MongoDB-style document stores, so either can stand in for the other; it is the intersection of what both can do.
SQL has multi-step transactions, but standalone MongoDB does not, so multi-step atomicity is a bonus rather than a requirement ([MetadataTransactions](../../.specs/specs/storage/spec.md#requirement-metadatatransactions)) and only single-step compare-and-swap is mandatory.
This is what lets the same code run on a laptop (SQLite) and a cluster (Postgres or Mongo).
A new metadata store either joins one of the two families and slots in through an existing scheme, or it forces a new contract.

#### Blob: content-addressed bytes, mostly a question of speaking S3

Blob storage keeps file bytes under a content hash, so identical content is stored once.
The contract is small: put, get, delete, exists, and list-all-hashes for garbage collection, with bytes returned verbatim ([BlobStoreProtocol](../../.specs/specs/storage/spec.md#requirement-blobstoreprotocol), [BlobEnumeration](../../.specs/specs/storage/spec.md#requirement-blobenumeration)), under a sharded `{hash[0:2]}/{hash[2:4]}/{hash}` key ([BlobPrefixDirectoryStructure](../../.specs/specs/storage/spec.md#requirement-blobprefixdirectorystructure)).
Object stores already do this, so the role reduces to one question: does the candidate speak S3?

#### Search: the index must commit inside the metadata transaction

Search decides most of the verdicts, because the contract allows only one way to do it well, and that way rules out every external engine.
Files can be searched five ways: glob (path patterns), find (metadata such as size, mtime, live-versus-deleted), regex (a substring in content), fulltext (whole words, ranked), and semantic (vector similarity).
Semantic is reserved but unbuilt, unsupported on every backend ([PluggableSearchProviders](../../.specs/specs/search/spec.md#requirement-pluggablesearchproviders)), so a vector-first candidate is scored against an empty slot.
Glob and find read only metadata, so every backend serves them; regex and fulltext need the contents indexed, and there are two paths.

The native path indexes text in the same transaction that writes the version ([NativeTextSearchStorage](../../.specs/specs/storage/spec.md#requirement-nativetextsearchstorage)).
A SQL store derives two indexes from one stored copy of the text: trigram for regex and substring, non-stemming word-tokens for fulltext ([FulltextWordRepresentation](../../.specs/specs/search/spec.md#requirement-fulltextwordrepresentation)).
A write is searchable the instant it commits; search answers from stored text with no blob reads; a missing or stale index fails loud and demands a reindex rather than guessing ([ColdIndexFailsLoud](../../.specs/specs/search/spec.md#requirement-coldindexfailsloud)).
An agent that writes a file and immediately searches for it must find it, and must never get a half-right answer.
The fallback path covers stores that cannot index, such as a standalone MongoDB ([PluggableSearchProviders](../../.specs/specs/search/spec.md#requirement-pluggablesearchproviders)): regex reads files one at a time up to a budget, and fulltext is unsupported.

Two facts drive every search verdict.
The index lives inside the metadata store and commits with the version, so the freshness model assumes the entry exists whenever it should ([SearchArtifactEnvelope](../../.specs/specs/search/spec.md#requirement-searchartifactenvelope)).
And search receives the permission-filtered set of visible versions and expands each match across it ([NativeTextSearchCapability](../../.specs/specs/search/spec.md#requirement-nativetextsearchcapability)), so the access check is part of search itself.
An external engine breaks both.
It runs as a separate server that updates after the write, so it lags and a search can miss a just-written file; and it returns its own hits, which must be re-filtered against the caller's visible set in code, a place to leak access.
Supporting one means a third, weaker regime the contract lacks.
The contract restricts the native capability to relational stores and bars document stores from it ([NativeTextSearchStorage](../../.specs/specs/storage/spec.md#requirement-nativetextsearchstorage)); an outside service sits further still from the model, directly at odds with the multi-tenant access-boundary priority.

#### Summary

Role fit is match, partial, or none.
"Fits current contract" is yes when the candidate is substitutable under an existing contract, no when it needs a new or changed one.

| Provider / family                                                                                                                         | Role assessed                | Contract it maps to                                          | Fits current contract?                                              | Verdict                                                                                       |
| ----------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------- | ------------------------------------------------------------ | ------------------------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| **Postgres and Postgres-wire** (Cockroach, Yugabyte, Cosmos-PG, AlloyDB, Neon, Supabase)                                                  | Metadata, plus native search | Relational floor; native search via `tsvector` and `pg_trgm` | Yes for metadata; native search yes only where the extensions exist | Metadata: match. Native search: partial, engine-dependent                                     |
| **SQLite**                                                                                                                                | Metadata, plus native search | Relational floor; FTS5 native search                         | Yes, shipped exemplar                                               | Match (the reference)                                                                         |
| **MySQL / MariaDB**                                                                                                                       | Metadata, plus native search | Relational floor, new adapter                                | Metadata yes; native search no, semantics differ                    | Metadata: match. Native search: non-match as specified                                        |
| **MongoDB and Mongo-wire** (Cosmos-Mongo, DocumentDB, FerretDB)                                                                           | Metadata                     | Document floor                                               | Yes, shipped exemplar                                               | Metadata: match. Native search: not applicable by design                                      |
| **DuckDB**                                                                                                                                | Metadata, or embedded search | Neither cleanly                                              | No                                                                  | Metadata: non-match (analytics engine, not point writes). Search: partial, only all-in-DuckDB |
| **S3 and S3-compatible** (MinIO, Ceph, SeaweedFS, Garage, R2, B2, Wasabi)                                                                 | Blob                         | `BlobStoreProtocol`                                          | Yes, covered by `s3://`                                             | Match                                                                                         |
| **Azure Blob / GCS**                                                                                                                      | Blob                         | `BlobStoreProtocol`, non-S3 wire                             | Contract yes; needs a new adapter                                   | Match, thin adapter                                                                           |
| **OPFS** (browser, Pyodide)                                                                                                               | Blob                         | `BlobStoreProtocol`, WASM adapter                            | Contract yes; needs an adapter and an in-browser stack              | Match, browser profile                                                                        |
| **pgvector / pgvectorscale**                                                                                                              | Search (semantic)            | In-transaction native path, extends Postgres                 | No semantic contract yet, but no new regime needed                  | Cleanest semantic path                                                                        |
| **sqlite-vec**                                                                                                                            | Search (semantic)            | In-transaction native path, extends SQLite                   | No semantic contract yet                                            | Local semantic, limited at scale                                                              |
| **Azure AI Search**                                                                                                                       | Search (fulltext, semantic)  | Out-of-band regime that does not exist yet                   | No, plus cloud-only                                                 | Fulltext and semantic capable; native in-transaction non-match                                |
| **External engines** (Qdrant, Weaviate, Milvus, Chroma, LanceDB, Firnflow, Elasticsearch/OpenSearch, Typesense, Meilisearch, Redis Stack) | Search (fulltext, semantic)  | Out-of-band regime that does not exist yet                   | No                                                                  | Capable, but not substitutable for the native path                                            |

#### Metadata candidates

##### Postgres and Postgres-wire-compatible

Postgres meets the metadata floor in full: ACID writes, compare-and-swap through `WHERE version_number = ?` ([MetadataCASSemantics](../../.specs/specs/storage/spec.md#requirement-metadatacassemantics)), a real transaction context, and literal prefix queries.
It also carries native search, with a `pg_trgm` index for regex and a `tsvector` for fulltext, both computed inline so no migration is needed ([FulltextMatchMode](../../.specs/specs/search/spec.md#requirement-fulltextmatchmode)).
The Postgres-wire family — CockroachDB, YugabyteDB, Cosmos DB for PostgreSQL, AlloyDB, Neon, Supabase, TimescaleDB — rides the same `postgresql://` adapter, satisfying the metadata contract with little or no new code, the way Cosmos for MongoDB rides the Mongo scheme ([URIBasedStoreResolution](../../.specs/specs/storage/spec.md#requirement-uribasedstoreresolution)).
Two per-engine checks shape the verdict.
The distributed engines (Cockroach, Yugabyte) default to serializable isolation and pay cross-node latency, so CAS holds but throughput and retries differ from single-node Postgres.
Native search needs both `pg_trgm` and `tsvector`: stock Postgres, Citus, AlloyDB, Neon, and Supabase have them, while Cockroach added them only recently.
An engine missing either is a metadata store with no native search, falling back to brute-force regex with fulltext unsupported.
_Verdict: metadata, match._
_Native search, match on stock Postgres and Citus, partial on the distributed forks pending an extension check._

##### SQLite

SQLite is the default local store and the search reference.
Embedded, with no operator, it meets the relational floor and provides both index representations through FTS5: a trigram table for substring and regex, and a `unicode61` word-token table for fulltext ([NativeTextSearchStorage](../../.specs/specs/storage/spec.md#requirement-nativetextsearchstorage)).
Everything else in this catalog is measured against it.
_Verdict: match, the reference._

##### MySQL / MariaDB

Metadata fits cleanly: InnoDB gives ACID writes, row locking, real transactions, and the compare-and-swap shape the floor needs.
The work is a new async adapter (`aiomysql` or `asyncmy`) plus a `mysql://` scheme, with no change to the floor (MariaDB also ships a native vector type, which only matters to the absent semantic slot).
Native search is where the fit breaks.
The contract wants non-stemming word tokens with no minimum length, so `s3` is matchable, plus a trigram index for substring and regex ([FulltextWordRepresentation](../../.specs/specs/search/spec.md#requirement-fulltextwordrepresentation), [NativeTextSearchStorage](../../.specs/specs/storage/spec.md#requirement-nativetextsearchstorage)).
MySQL and MariaDB fulltext indexes default to a minimum token length, carry stopwords, split natural-language from boolean matching, and have no `pg_trgm` equivalent for arbitrary substring search; mapping those onto the contract's two representations would change what a query matches.
So as specified, the cleanest fit is a metadata store with no native search — regex falls back to brute force, fulltext is unsupported — unless the search contract grows a MySQL-shaped representation.
_Verdict: metadata, match with a new adapter._
_Native search, non-match under the current representation contract._

##### MongoDB and Mongo-wire-compatible

MongoDB is the document-family reference, defining the other half of the floor.
Compare-and-swap runs through `find_one_and_update` with a version match ([MetadataCASSemantics](../../.specs/specs/storage/spec.md#requirement-metadatacassemantics)), and transactions are best-effort, real only on a replica set, which is why multi-document atomicity sits outside the floor ([MetadataTransactions](../../.specs/specs/storage/spec.md#requirement-metadatatransactions)).
By design it exposes no native search: `native_text_search()` returns `None`, regex falls back to brute force, fulltext is unsupported — Mongo has a text index and Atlas Search, but the contract keeps them out to hold the document family at the floor.
The Mongo-wire family rides `mongodb://`: Azure Cosmos DB for MongoDB (request-unit limits, version-specific transaction caps), Amazon DocumentDB, and the Postgres-backed FerretDB — verify transaction support and wire version per target.
_Verdict: metadata, match on the document floor._
_Native search, not applicable by design._

##### DuckDB

DuckDB fits neither role cleanly.
It is an embedded analytics engine: columnar, tuned for bulk scans, single-writer with coarse concurrency.
The metadata workload is the opposite (a row per version, concurrent writers, compare-and-swap on every write), so per-version point writes are an anti-pattern and it is a poor metadata store.
Its search angle is real but narrow: a fulltext extension and a vector-similarity extension exist, but only in an all-in-DuckDB design where it is both store and index — a different architecture, not a drop-in for either role.
_Verdict: metadata, non-match (analytics, not point writes)._
_Search, partial, only inside an all-DuckDB design the current regimes do not cover._

#### Blob candidates

##### S3 and S3-compatible

S3 is the blob reference.
It maps straight onto the contract — verbatim bytes under the sharded key layout, idempotent put, enumeration for garbage collection ([BlobStoreProtocol](../../.specs/specs/storage/spec.md#requirement-blobstoreprotocol)) — with the diskcache wrapper enabled automatically for remote stores ([BlobCaching](../../.specs/specs/storage/spec.md#requirement-blobcaching)).
Most self-hosted object stores are free here.
MinIO, Ceph (RADOS gateway), SeaweedFS, Garage, Cloudflare R2, Backblaze B2, and Wasabi all expose the S3 API, so the existing `s3://` adapter covers them with no new code — the blob counterpart to the Postgres-wire and Mongo-wire groupings; confirm signature version and multipart quirks per implementation.
_Verdict: match._

##### Local filesystem

The local filesystem is the default blob exemplar (`file:///`), using the same content-hash layout on disk.
It is the baseline the remote stores are measured against.
_Verdict: match._

##### Azure Blob and GCS

Both fit the contract; only the wire protocol differs.
Each stores verbatim bytes under an arbitrary key, so the semantics map cleanly: idempotent put, exists, enumerate, sharded keys.
Neither speaks S3, so each needs a thin adapter and scheme (`az://`, `gs://`).
GCS offers an S3-compatible XML API that could ride `s3://` in a pinch, but a native adapter is cleaner.
The change is purely additive, with no change to the floor.
_Verdict: match, thin new adapter._

##### OPFS (Origin Private File System)

OPFS is the blob target for a browser or WASM profile (§1.3 and §2.3).
It gives an origin-scoped sandboxed file store reachable from WASM, and verbatim bytes keyed by content hash satisfy the contract.
The frictions are about deployment, not the contract: async-only, quota-limited, origin-scoped, no server-side sharing, and useful only if the rest of the stack also runs in the browser (e.g. SQLite-WASM for metadata).
It is a workable adapter, but part of a larger client-side deployment story rather than a standalone swap.
_Verdict: match for a browser profile, new adapter, tied to a WASM deployment design._

#### Search candidates

##### In-transaction vector: pgvector and sqlite-vec

Only an engine that _is_ the metadata store can index in the version's transaction ([NativeTextSearchStorage](../../.specs/specs/storage/spec.md#requirement-nativetextsearchstorage)), which means a Postgres or SQLite extension.
These extend search without inventing a new consistency regime, though they still need the semantic contract written, and they add an embedding step.
Embeddings are derived artifacts, so they must be reproducible, with the model and dimensions folded into the index's parameter hash, and they default to CPU unless a GPU cost case is made.
**pgvector / pgvectorscale** runs vector similarity inside Postgres (HNSW and IVFFlat indexes; pgvectorscale adds a disk-based ANN index for larger corpora).
It is the cleanest path to semantic search: an embedding keyed by content hash commits in the version transaction and behaves as a sibling of the native capability, inheriting its freshness model unchanged — content-addressed identity, a fresh index that answers authoritatively, a stale one that fails loud.
No new regime is needed; the costs are the semantic contract and the embedding pipeline, and it exists only where Postgres holds the metadata.
**sqlite-vec** is the same idea for the local profile: vector search as a SQLite extension, written in transaction, doing flat brute-force nearest-neighbor with no ANN graph.
It holds at small scale and weakens as the corpus grows, keeping the property that a laptop runs the same contract as production.
The current Postgres `tsvector`/`pg_trgm` and SQLite FTS5 indexes are this tier's reference, not new candidates.
_Verdict: the substitutable semantic path, pending a semantic contract and an embedding pipeline; pgvector for servers, sqlite-vec for local, scale-limited._

##### External engines: capable, but each needs a new regime

Qdrant, Weaviate, Milvus, Chroma, LanceDB, Firnflow, Elasticsearch and OpenSearch, Typesense, Meilisearch, and Redis Stack all run as their own store, separate from the metadata store.
None can be the native capability, which is in-store and in-transaction by definition ([NativeTextSearchStorage](../../.specs/specs/storage/spec.md#requirement-nativetextsearchstorage)).
Each would plug into the async search-provider slot ([SearchProviderProtocol](../../.specs/specs/search/spec.md#requirement-searchproviderprotocol)) that the metadata-only default provider occupies today, which opens the same three gaps for every engine.
Freshness breaks first.
The native model assumes the index entry is present whenever a file's artifact is current, because they were written together ([SearchArtifactEnvelope](../../.specs/specs/search/spec.md#requirement-searchartifactenvelope)); an eventually consistent index breaks read-after-write and lags silently instead of failing loud.
Closing this needs a new provider regime with an explicit weaker contract, or a readability check plus a pending artifact state.
Access control breaks second.
The native path expands a match across the permission-filtered versions it was handed ([NativeTextSearchCapability](../../.specs/specs/search/spec.md#requirement-nativetextsearchcapability)), but an external engine returns its own hits, which must be re-filtered against the visible set in code, or filtered by pushing visible version IDs into the query and risking the engine's filter-size limits.
Either way is a place a visibility leak can occur, and access control is the system's first priority — the sharpest edge under multi-tenant B2C + B2B.
Regex breaks third.
The contract's regex is exact substring matching over stored bytes on a trigram index, which token-based engines approximate awkwardly through keyword-tokenized fields and slow, complexity-limited wildcard or regex queries (the mismatch documented for Azure AI Search below).
In return they offer what the contract does not yet require: hybrid ranking that fuses keyword and vector scores, and approximate nearest-neighbor search at scale.
That makes them the natural home for a future semantic or hybrid contract, not a substitute for the native fulltext and regex path.

| Engine                       | Self-host (license)             | Embedded mode            | Lexical    | Vector / ANN        | Hybrid                | Notes for contract fit                                                                                                                                                                    |
| ---------------------------- | ------------------------------- | ------------------------ | ---------- | ------------------- | --------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Qdrant**                   | Yes (Apache-2.0)                | No, server               | Weak       | Yes (HNSW)          | Yes, sparse and dense | Strong payload filtering eases in-code permission filtering                                                                                                                               |
| **Weaviate**                 | Yes (BSD-3)                     | No, server               | Yes (BM25) | Yes                 | Yes                   | Modules for embedding generation                                                                                                                                                          |
| **Milvus**                   | Yes (Apache-2.0)                | Lite (embedded)          | Weak       | Yes, many ANN types | Partial               | Heavy distributed operations at scale; vector-first                                                                                                                                       |
| **Chroma**                   | Yes (Apache-2.0)                | Yes, in-process          | Weak       | Yes                 | Partial               | Simplest; small scale; embedded mode fits the local profile                                                                                                                               |
| **LanceDB**                  | Yes (Apache-2.0)                | Yes, object-store-native | Yes (FTS)  | Yes                 | Yes                   | Distinct: Lance columnar on local or S3 fits the laptop-to-S3 story, but still out-of-band from the metadata transaction                                                                  |
| **Firnflow** (LanceDB-based) | Yes (Apache-2.0)                | Yes, Python embed        | Yes (BM25) | Yes, + multivector  | Yes                   | Productized LanceDB: object-store-native L1/L2/L3 tiering (S3/R2/GCS) and near-zero-idle multi-tenancy ease the access-boundary edge, but still out-of-band from the metadata transaction |
| **Elasticsearch**            | Yes (SSPL/Elastic, AGPL option) | No, server               | Strong     | Yes (kNN)           | Yes                   | Closest self-hosted analogue to Azure AI Search; heavy operations                                                                                                                         |
| **OpenSearch**               | Yes (Apache-2.0)                | No, server               | Strong     | Yes (kNN)           | Yes                   | The permissively licensed Elasticsearch fork                                                                                                                                              |
| **Typesense**                | Yes (GPL-3.0)                   | No, server               | Yes        | Yes                 | Yes                   | Lightweight and fast; small footprint                                                                                                                                                     |
| **Meilisearch**              | Yes (MIT)                       | No, server               | Yes        | Yes, newer          | Yes                   | Lightweight; vector and hybrid still maturing                                                                                                                                             |
| **Redis Stack (RediSearch)** | Yes (RSALv2/SSPL)               | No, server               | Yes        | Yes                 | Partial               | Attractive only when Redis is already in the stack                                                                                                                                        |

_Verdict: individually capable, and the right substrate for a future hybrid or semantic contract, but not substitutable for the native, in-transaction fulltext and regex path._
_Each needs the same new out-of-band regime, and vector use needs the semantic contract._

##### Azure AI Search

Azure AI Search is an out-of-band, cloud-only member of the external-engine group, with one extra constraint: no self-hosted or embedded mode, so it can never be the local profile.
Its fulltext (BM25, all-terms and any-term modes) maps well and arguably exceeds the contract.
Its semantic search (vectors, hybrid ranking, a reranker) is its strength and the main reason to reach for it if a semantic contract gets written.
Its regex is an impedance mismatch, and it cannot be the native capability because it is non-transactional and eventually consistent.
_Verdict: fulltext and semantic capable; native in-transaction non-match; needs the new regime; cloud-only._

#### Weaker fits, set aside

| Candidate                            | Considered as       | Set aside because                                                                                                                                                                                                                                                 |
| ------------------------------------ | ------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **DuckDB as metadata**               | Metadata store      | Columnar, single-writer, tuned for analytics; per-version point writes are an anti-pattern. Fails the workload, not the protocol shape. Its search angle is noted above.                                                                                          |
| **Redis as durable metadata**        | Metadata store      | In-memory first, so durability and consistency do not meet the metadata floor. Works only as the cache layer, or through RediSearch as an out-of-band search engine.                                                                                              |
| **Cassandra / ScyllaDB**             | Metadata store      | Wide-column model does not fit path-prefix listing. Compare-and-swap exists through lightweight transactions, but adopting it adds a third metadata family and renegotiates the floor.                                                                            |
| **DynamoDB**                         | Metadata store      | Conditional writes give compare-and-swap, but it is cloud-only key-value, neither Postgres- nor Mongo-wire, so a new family and adapter with no native-search story.                                                                                              |
| **FoundationDB**                     | Metadata store      | A strong transactional ordered key-value store, but no SQL or Mongo wire, so projecting files, versions, and permissions onto raw key-value is a large adapter.                                                                                                   |
| **Firestore**                        | Metadata store      | Cloud document store, not Mongo-wire, so a new family, with eventual-consistency edges to verify.                                                                                                                                                                 |
| **Neo4j and graph stores**           | Metadata store      | Graph model does not fit the file, version, and permission shape.                                                                                                                                                                                                 |
| **SurrealDB / ArangoDB / Couchbase** | Metadata and search | Multi-model engines tempt one store into two roles, the floor-violating union the contract warns against. Each role is weaker than a dedicated store, and adopting one renegotiates the metadata family. Revisit only if single-engine deployment becomes a goal. |
| **Pinecone**                         | Search (vector)     | Cloud-only managed vector store, ruled out by the self-hostable preference; otherwise an out-of-band group member.                                                                                                                                                |
| **Vespa**                            | Search              | Powerful but operationally heavy; folds into the out-of-band group without changing the verdict.                                                                                                                                                                  |
| **Manticore / Sphinx**               | Search              | Lexical engine with no contract advantage over OpenSearch or Typesense; an out-of-band group member.                                                                                                                                                              |

#### Bottom line: search is the only constrained role

Metadata has two families and a literal floor.
New relational engines (MySQL, MariaDB) and wire-compatible ones (the Postgres-likes, the Mongo-likes) slot in without renegotiating the floor; everything else — wide-column, graph, key-value, cloud document — adds a third family.
Blob is almost entirely whether a store speaks S3: the self-hosted object stores come for free, and Azure Blob, GCS, and OPFS are thin additive adapters.
Search is the constraint: the contract defines a native in-transaction path and a brute-force fallback, and no third path.
Only Postgres and SQLite extensions (pgvector, sqlite-vec) extend search natively, and even they need the semantic contract written first.
Every external engine — Azure AI Search and the whole out-of-band group — is a capable index that needs a new, weaker provider regime, plus in-code permission filtering to hold the access boundary.
The decision this sets up: adding vector or semantic search through a Postgres or SQLite extension stays inside the existing consistency model, while any external engine introduces a new one.
Under a multi-tenant B2C + B2B access boundary, that difference, more than raw capability, is what matters.

### 3.3 ZeroFS and FUSE-mounted virtual filesystems don't fit ai-vfs

ZeroFS and similar FUSE-backed, S3-native POSIX filesystems solve a different layer of problem than ai-vfs does, and adopting one anywhere in the architecture — the blob store, the sandboxed-execution boundary, or the multi-tenant isolation boundary — works against ai-vfs's core bet rather than reinforcing it.
The one exception is narrow: a single-session, single-tenant, ephemeral mount (an agent's offline/local working copy, or a scoped export) sidesteps most of the mismatch because the mount unit and the tenant unit are 1:1.
This verdict is anchored directly to the thesis and the multi-tenant differentiator, not to a spec clause.

#### What ai-vfs is, and what ZeroFS is

ai-vfs is a governed metadata layer over dumb storage: a `MetadataStore` (SQLite/Postgres/Mongo) holds immutable, content-addressed, CAS-versioned file records, sitting on a trivial `BlobStore` (local FS/S3) that does nothing more than content-hash `put`/`get`/`exists`.
The product's value is the layer above storage — permissions, audit, rollback, retention and GC — not the storage itself.
The execution layer is explicit that the sandbox's filesystem calls are intercepted at the interpreter level: the real OS filesystem is never attached or exposed to the sandbox, and every path operation must transit the governed FS-port so permission and audit checks fire.
ZeroFS is the opposite kind of thing.
It is an actual POSIX filesystem (NFS/9P/NBD/FUSE) that makes an S3 bucket look like a real disk, with leader-standby HA, conditional-write fencing for single-writer safety, and `fsync` as the durability boundary, built to give unmodified software (databases, VM disks, build systems) a durable POSIX view of object storage.
Other FUSE-mounted-virtual-filesystem projects — s3fs-fuse, JuiceFS, mountpoint-s3, TigerFS — share the same shape: a kernel-mediated mount exposing POSIX semantics over cloud storage.

#### Mismatch 1: wrong layer for the blob/execution boundary

ai-vfs's value is the CAS, audit, and versioning metadata layer.
ZeroFS solves durability and availability of raw POSIX storage, a problem ai-vfs's content-addressed, write-once blob store does not have: it needs no in-place mutation, no POSIX locks, and no directory semantics on blobs.
Putting ZeroFS under the execution/sandbox boundary so agent code sees a "real" filesystem would bypass the permission, audit, and versioning checks that interpreter-level interception exists specifically to enforce — directly against the thesis bet that every change is reversible, attributable, and contained. ai-vfs's default profile is a pip-installable library with in-process interpreters and no required VM or container.
ZeroFS is a separate daemon needing mounts, a replication topology, and, per its own design, Redis for conditional-write fencing on non-S3-native backends: a much heavier deployment footprint than the problem calls for.
And ZeroFS's durability machinery (lineage tokens, the `fsync` boundary, memtable batching) exists to make large in-place mutating writes durable, but ai-vfs versions are whole-object, immutable, and CAS'd, so there is no in-place mutation to make durable in the first place.

#### Mismatch 2: wrong granularity for multi-tenant isolation

ai-vfs's multi-tenant model is per-request, per-path, application-layer.
Every operation carries a `(namespace_id, principal_id, operation_type, path)` tuple, checked against a DB-row permission table with default-deny, most-specific-prefix-wins, and invisible pruning (unauthorized paths don't appear in listings), enforced in one Python choke point inside a single stateless process serving arbitrary tenants concurrently.
FUSE-mounted filesystems are per-mount, OS-level: a mount is a kernel object with one POSIX identity model (uid/gid/mode bits).
That is the fundamental unit mismatch. ai-vfs wants one shared process with N tenants disambiguated per call; FUSE wants one mount, one identity.
Concretely, for a B2C/B2B cloud application, five problems follow.

Mount-per-tenant doesn't scale to web-request concurrency.
The natural way to get FUSE tenant isolation is one mount per namespace or per principal — fine for a desktop client with one logged-in user (Dropbox-style), not fine for a SaaS backend serving thousands of orgs from a shared app-server fleet, where it would mean thousands of live FUSE daemons and kernel channels held open concurrently.
Mount and unmount lifecycle, stale-mount cleanup, and per-mount OS resource limits become an ops problem a `WHERE namespace_id = ?` query never has.
FUSE's identity primitive can't express the permission model.
POSIX gives uid/gid/mode: no path-prefix grants, no `{read, write, delete, execute, admin}` per subtree, no most-specific-prefix resolution, no runtime-revocable per-principal ACLs.
Reproducing this would mean reimplementing the entire access-control model inside the FUSE driver's `getattr`/`readdir`/`open` callbacks, which are typically Rust, C, or Go while the app's permission model lives in Python: duplicated security logic instead of one auditable choke point.
Invisible pruning needs caller identity per call, which FUSE doesn't naturally have in a multi-tenant server.
`readdir` would need to filter results per calling principal, but a FUSE server only sees the OS uid of the calling process, with no clean way to carry "agent principal ULID in tenant org X" through a kernel-mediated syscall short of provisioning one uid per principal on the host — its own scaling wall.
Isolation-at-rest doubles. ai-vfs already isolates tenants at the metadata row level, with an accepted, documented risk around shared content-addressed blobs across namespaces; a ZeroFS-backed mount would need its own per-tenant bucket, prefix, or key scheme to get equivalent isolation at the storage layer, leaving two places tenant isolation must stay correct and in sync.
Versioning collides with in-place POSIX writes.
To preserve ai-vfs's version history, every FUSE write would have to be intercepted and turned into a new version record anyway, rebuilding the governed write path underneath a POSIX illusion for no benefit over calling it directly.
And the trust boundary grows. ai-vfs's current design has zero kernel exposure (DB rows and object-storage keys, auditable in a debugger), while packing many tenants' FUSE mounts onto shared multi-tenant compute adds a kernel-mediated attack surface — a FUSE driver bug or resource exhaustion in one mount can affect the host — that the current architecture doesn't have to reason about at all.

#### Where FUSE-style mounting could legitimately fit

The pattern works when the mount unit and the tenant unit are naturally 1:1, not when a shared service multiplexes many tenants.
A single agent's local or offline session qualifies: running disconnected, say on a laptop or an ephemeral VM, and syncing back to ai-vfs later is one mount, one namespace, one principal, torn down when the session ends, so none of the scaling or identity problems apply.
A scoped, ephemeral export or interop path for one principal works too — a customer who wants to `rsync` or point an unmodified third-party tool at their own namespace, under an explicit narrow grant, for one session, materialized on demand, read-mostly, and reconciled back through the governed write path afterward rather than left live and multi-tenant.
Both are edge cases bolted onto one specific, non-default execution or export path, not a replacement for the namespace-plus-principal-plus-permission model that carries multi-tenancy today.
For the core product (many orgs, many concurrent web requests, one shared backend), the DB-row-based model is the better-fitting design; FUSE-style mounting would trade a small, auditable trust boundary for a much larger and harder-to-reason-about one.

#### Secondary considerations: ZeroFS ideas that are interesting but marginal

Two of ZeroFS's ideas are worth noting even though neither changes the verdict.
Streaming large blobs is unimplemented today (`BlobStoreProtocol.put_stream`/`get_stream` currently raise `NotImplementedError`), but if large-file support becomes a real need, S3's own multipart API gets there faster than adopting a whole POSIX filesystem layer.
And ZeroFS's HA leader-standby pattern for metadata (conditional-write leader election that prevents split-brain without clock sync) is a neat design, but ai-vfs's Postgres and Mongo metadata stores already have mainstream, better-supported HA options at the DB layer.
Adopting a bespoke replication protocol here would be a step backward in maturity from what is already available.
