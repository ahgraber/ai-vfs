# AI Virtual Filesystem: Project Landscape Survey

Date: 2026-04-04

---

## 1. fsspec/filesystem_spec

**What it is:** A Python library providing a unified interface to local, remote, and embedded file systems.
The de facto standard for filesystem abstraction in the Python data ecosystem (used by Dask, pandas, zarr, PyArrow).

**Key architecture decisions:**

- Defines an `AbstractFileSystem` base class with ~30 methods (`ls`, `cat`, `mkdir`, `open`, etc.) that all backends must implement.
- `AbstractBufferedFile` provides random-access reads without downloading entire files -- critical for big-data workloads.
- URL chaining via `::` separator enables composable layers (e.g., `simplecache::zip://*.csv::s3://bucket/file.zip`).
- Filesystem instances are serializable (no locks, no open file handles) so they can be shipped across a Dask cluster.
- Built-in caching strategies: file cache, block cache, and simple cache with configurable expiry.
- Supports transactions for semi-atomic multi-file writes.
- Any filesystem can be FUSE-mounted via `fsspec.fuse.run()`.
- Key-value store view via `fsspec.get_mapper()` -- any path becomes a dict-like mapping.
- Async support: `AsyncFileSystem` subclasses expose coroutine methods with auto-generated sync wrappers.

**Relevance to AI-VFS:** fsspec is the most direct foundation.
Its abstract interface is already the lingua franca for Python filesystem access.
An AI virtual filesystem could implement the fsspec interface to get instant compatibility with the entire Python data stack.
The URL chaining and caching layers demonstrate how to compose virtual filesystem layers.
JuiceFS already ships an fsspec-compatible Python SDK.

---

## 2. juicedata/juicefs

**What it is:** A high-performance distributed POSIX file system that decouples metadata (stored in Redis, PostgreSQL, TiKV, MySQL, SQLite) from data (stored in any object storage: S3, GCS, MinIO, Ceph).
Apache-2.0 licensed, written in Go.

**Key architecture decisions:**

- Three-component architecture: Client (handles all I/O, runs FUSE or SDK), Metadata Engine (pluggable: Redis for speed, PostgreSQL for ACID), Data Storage (any S3-compatible object store).
- Files are split into 64 MB chunks, each chunk contains slices (representing individual writes), and slices are split into 4 MB blocks for parallel upload.
- Metadata-data separation enables independent scaling: swap Redis for TiKV to handle billions of files, or switch S3 providers without touching metadata.
- Provides multiple access methods: FUSE mount, Python SDK (fsspec-compatible), Hadoop Java SDK, Kubernetes CSI driver, S3 Gateway, WebDAV server.
- Strong consistency by default, with tunable metadata caching for read-heavy workloads.
- Automatic compaction merges fragmented slices within chunks to maintain read performance.
- Built-in trash/versioning with configurable retention.

**Relevance to AI-VFS:** JuiceFS demonstrates the power of separating metadata from data in a filesystem.
For an AI virtual filesystem, this pattern enables: (a) storing file metadata in a fast database while keeping blobs in cheap object storage, (b) supporting multiple access protocols from a single source of truth, (c) pluggable metadata backends per deployment.
The fsspec-compatible Python SDK shows this can integrate cleanly into the Python ecosystem.

---

## 3. vercel-labs/just-bash

**What it is:** A virtual bash environment with an in-memory filesystem, written in TypeScript, designed for AI agents. 2.3k stars.
Provides broad support for standard Unix commands and bash syntax without spawning real processes.

**Key architecture decisions:**

- All commands (75+) are reimplemented in TypeScript -- no `fork`/`exec`, no real shell.
- Four filesystem backends: InMemoryFs (default, pure memory), OverlayFs (copy-on-write over real directory), ReadWriteFs (direct disk access), MountableFs (compose multiple FS at different mount points).
- Each `exec()` call gets isolated shell state (env vars, cwd reset) but the filesystem is shared across calls.
- Network access disabled by default; when enabled, enforced via URL-prefix allowlists with header transforms for credential injection.
- Optional Python (CPython-to-WASM) and JavaScript (QuickJS-to-WASM) execution, off by default.
- AST transform plugins allow instrumenting bash scripts before execution.
- Configurable execution limits: max call depth, command count, loop iterations.
- Ships as both a library (`just-bash`) and an AI SDK tool (`bash-tool`), plus a `Sandbox` class that's API-compatible with `@vercel/sandbox`.
- Runs in both Node.js and browser environments.

**Relevance to AI-VFS:** just-bash is a direct precedent for giving AI agents a sandboxed filesystem environment.
The MountableFs pattern (composing in-memory, overlay, and real FS at different paths) is directly applicable.
The OverlayFs (reads from disk, writes stay in memory) is particularly relevant for agent workspaces where you want agents to see real project files but contain their mutations.
The `defineCommand` extensibility shows how to add domain-specific operations to a virtual environment.

---

## 4. everruns/bashkit

**What it is:** A virtual bash interpreter with a virtual file system for multi-tenant environments, written in Rust. 100+ stars.
Conceptually similar to just-bash but implemented in Rust with PyO3/NAPI-RS bindings.

**Key architecture decisions:**

- All 150 commands reimplemented in Rust -- zero process spawning, fully in-process execution.
- POSIX-compliant (IEEE 1003.1-2024 Shell Command Language) with extensive bash extensions (arrays, associative arrays, brace expansion, extended globs, process substitution, coprocesses).
- Same VFS layering as just-bash: InMemoryFs, OverlayFs, MountableFs, with optional RealFs backend.
- Defense-in-depth security: 60+ identified threats across 11 categories, each with mitigation and test coverage.
  Parser limits (timeout, fuel budget, AST depth), filesystem limits (total bytes, file count), panic recovery via `catch_unwind`.
- LLM tool contract: `BashTool` with streaming output, system prompts, and discovery metadata -- designed to be directly consumed by LLM tool-calling protocols.
- Scripted tool orchestration: compose multiple tool definitions into multi-tool bash scripts.
- MCP server mode via `bashkit mcp`.
- Experimental: virtual git operations on VFS, embedded Python via Monty, embedded TypeScript via ZapCode.
- Language bindings: Python (PyO3), JavaScript/TypeScript (NAPI-RS for Node/Bun/Deno).
- Multi-tenant: each `Bash` instance is fully isolated.

**Relevance to AI-VFS:** Bashkit represents the most complete vision of a sandboxed virtual environment for AI agents.
Its VFS + bash interpreter + embedded language runtimes (Monty for Python, ZapCode for TypeScript) effectively creates a complete virtual OS layer.
The Rust implementation gives better performance and memory safety than just-bash's TypeScript.
The MCP server mode and LLM tool contract show how to expose a virtual filesystem to AI agents via standard protocols.
The integration of Monty for Python execution within the VFS is directly relevant -- files written by bash are readable from Python and vice versa.

---

## 5. pydantic/monty

**What it is:** A minimal, secure Python interpreter written from scratch in Rust for running LLM-generated code. 6.6k stars.
Uses Ruff's parser for Python source, compiles to its own bytecode format, and executes in a custom VM.

**Key architecture decisions:**

- Not CPython-with-restrictions and not Python-compiled-to-WASM.
  A from-scratch bytecode VM in Rust.
- Zero access by default: no filesystem, no network, no env vars.
  All external interaction happens through explicitly provided external functions.
- Sub-microsecond startup (~0.06ms) because it is embedded in the host process.
  CPython's `exec()` takes ~0.1ms; Docker takes ~195ms; sandbox services take ~1000ms+.
- Snapshotting: execution state can be serialized to bytes mid-flight (at external function call boundaries), stored, and resumed later in a different process.
  Snapshots are single-digit kilobytes vs. gigabytes for VM snapshots.
- Iterative execution model: `start()` runs until an external function call, returns a `FunctionSnapshot`, host resolves the call, then `resume()` continues.
  This enables pause/resume workflows (e.g., waiting for human approval).
- Type checking via bundled `ty` in a single binary.
- Callable from Rust, Python, or JavaScript.
- Resource limits: memory usage, allocations, stack depth, execution time.
- Limited stdlib: `sys`, `os`, `typing`, `asyncio`, `re`, `datetime`, `json`.
  No classes yet, no third-party packages (by design).
- Already integrated into bashkit as the embedded Python runtime.

**Relevance to AI-VFS:** Monty is the execution engine complement to a virtual filesystem.
Where fsspec/just-bash/bashkit provide the filesystem abstraction, Monty provides the sandboxed compute layer.
Its external-function-only design maps perfectly to a VFS: expose `read_file`, `write_file`, `list_dir` as external functions, and the LLM's Python code can interact with the virtual filesystem without any real filesystem access.
The snapshotting capability enables persisting agent execution state alongside filesystem state.
Pydantic AI's CodeModeToolset demonstrates the pattern: instead of sequential tool calls, the LLM writes Python that calls tools as functions, executed safely by Monty.

---

## 6. timescale/tigerfs

**What it is:** A filesystem backed by PostgreSQL that exposes database tables and rows as files and directories.
Written in Go. 200+ stars.
Mounts via FUSE on Linux and NFS on macOS.

**Key architecture decisions:**

- Two modes: File-first (write markdown/files, stored as Postgres rows with frontmatter as columns) and Data-first (mount existing Postgres DB, explore with `ls`/`cat`/`grep`).
- Architecture: Unix Tools -> FUSE/NFS -> TigerFS Daemon -> PostgreSQL.
- Every file is a real PostgreSQL row.
  Directories are tables.
  File contents are columns.
- Pipeline query paths pushed down to SQL: `.by/customer_id/123/.order/created_at/.last/10/.export/json` resolves as a single optimized SQL query.
- ACID transactions for all operations: `mv` is atomic, multiple agents can read/write concurrently without coordination.
- Automatic version history via TimescaleDB hypertables: every edit captured as a timestamped snapshot under `.history/`.
- Apps system: write "markdown,history" to `.build/` and a table becomes a directory of `.md` files with YAML frontmatter.
- Multi-tenant agent coordination patterns: task queues via `mv` between `todo/doing/done` directories, shared knowledge bases with immediate visibility.

**Relevance to AI-VFS:** TigerFS is the strongest demonstration of "the filesystem is the API" for AI agents.
Its core insight -- that agents already understand files, so expose everything as files -- is foundational to an AI-VFS project.
Key patterns to adopt: (a) database-backed filesystem for ACID guarantees and concurrent multi-agent access, (b) version history for every file change, (c) pipeline query paths that translate filesystem operations to optimized backend queries, (d) the task-queue-as-directories pattern for agent coordination.
The main limitation is the tight coupling to PostgreSQL.

---

## 7. eryx-org/eryx

**What it is:** A Rust library that executes Python code in a WebAssembly sandbox via Wasmtime with async callbacks. 45 stars.
Embeds CPython 3.14 compiled to WASI.

**Key architecture decisions:**

- Uses real CPython 3.14 compiled to WASM (from the Bytecode Alliance's componentize-py), not a reimplemented interpreter like Monty.
- Async callback mechanism: host Rust functions exposed as `await`-able Python functions.
- Sandbox pooling: managed pool of warm sandbox instances for high-throughput scenarios with pre-warming, bounded concurrency, and idle eviction.
- Pre-compilation: 41x faster sandbox creation (~16ms vs ~650ms) via ahead-of-time Wasm compilation.
- Session state persistence: variables, functions, classes persist between executions (REPL-style).
- State snapshots via pickle-based serialization.
- Host-controlled networking: TCP/TLS with per-host allowlists, disabled by default.
- Native extension support (experimental): numpy and similar via late-linking.
- Execution cancellation via Wasmtime's epoch-based interruption.
- Composable runtime libraries: pre-built Python APIs with type stubs.

**Relevance to AI-VFS:** Eryx represents a different point on the sandbox spectrum than Monty: full CPython compatibility (classes, stdlib, some packages) at the cost of higher startup latency (~16ms pre-compiled vs ~0.06ms Monty).
For an AI-VFS, eryx would be relevant if agents need to run code with third-party packages or full Python semantics.
The sandbox pooling pattern is valuable for multi-tenant deployments.
The callback mechanism for exposing host functions to sandboxed Python is similar to Monty's external functions and could be used to bridge VFS operations into the sandbox.

---

## 8. inducer/starlark-pyo3

**What it is:** Python bindings (via PyO3) for starlark-rust, exposing the Starlark configuration language to Python. 41 stars, but 2.4M monthly PyPI downloads.

**Key architecture decisions:**

- Starlark is a Python-like configuration language (created by Google for Bazel) with deliberate restrictions: deterministic evaluation, hermetic execution (no filesystem, network, or system clock access), parallel-safe (shared data becomes immutable).
- Bindings are straightforward: expose starlark-rust's `Module`, `Globals`, evaluation to Python via PyO3.
- Supports Decimal type, positional-only arguments, and full Starlark specification.
- Very fast startup (~1.7ms) since it runs embedded in the process.
- No file handling by design; no snapshotting capability.

**Relevance to AI-VFS:** Limited direct relevance.
Starlark is a configuration language, not a general-purpose scripting language -- it lacks classes, exceptions, async, and file I/O.
However, it demonstrates the principle of a deliberately restricted, deterministic execution environment accessible from Python.
Could be relevant for evaluating deterministic configuration or policy expressions within an AI-VFS (e.g., access control rules, file routing policies) where you want guaranteed termination and reproducibility.
Monty has largely superseded starlark-pyo3 for the "safe LLM code execution" use case.

---

## 9. mickael-kerjean/filestash

**What it is:** A self-hosted file management platform and universal data access layer. 13.9k stars.
Written in Go with a vanilla JS frontend.

**Key architecture decisions:**

- Plugin-driven architecture: everything is a plugin -- storage backends, authentication, authorization, file viewers, search, thumbnailing, frontend patches, middleware, endpoints.
- Core `IBackend` interface is minimal: `Ls`, `Stat`, `Cat`, `Mkdir`, `Rm`, `Mv`, `Save`, `Touch` -- any storage that implements these 8-10 methods becomes a first-class backend.
- Supports 20+ storage protocols: FTP, SFTP, S3, SMB, WebDAV, IPFS, NFS, and virtual filesystems.
- Multiple access gateways: web client, SFTP gateway, MCP gateway, S3 gateway.
- Workflow engine: chain actions on file events (notifications, MFT pipelines).
- Extensive file-type support: 200+ formats across photography, science, GIS, data engineering, biomedical, 3D, etc.
- No FUSE dependency -- operates without kernel-level filesystem mounting.
- RBAC authorization with pluggable authentication (including delegating to external systems like WordPress).

**Relevance to AI-VFS:** Filestash's architecture is highly relevant as a reference for building a universal data access layer.
Its minimal `IBackend` interface (8 methods) proves that a simple contract can unify wildly different storage systems.
The plugin architecture shows how to keep the core minimal while supporting diverse backends and file types.
The MCP gateway is particularly interesting -- it means filestash already supports exposing its unified filesystem to AI agents via the Model Context Protocol.
The workflow engine (actions triggered by file events) is relevant for building reactive agent behaviors on top of a VFS.

---

## 10. c4pt0r/agfs

**What it is:** Aggregated File System (Agent FS) -- a system that unifies backend services (KV stores, message queues, databases, object storage) as filesystem operations. 331 stars.
Written in Go/C++/Python/Rust.
Tribute to Plan 9.

**Key architecture decisions:**

- Core philosophy: `redis.set("key", "val")` becomes `echo "val" > /kvfs/keys/mykey`; `sqs.send_message(queue, msg)` becomes `echo "msg" > /queuefs/q/enqueue`.
- Multiple virtual filesystems composed together: `kvfs/` (key-value), `queuefs/` (message queues), `s3fs/` (object storage), `sqlfs/` (databases), `heartbeatfs/` (agent liveness), `memfs/` (in-memory).
- SQL access via Plan 9-style session interface: create session via `cat ctl`, write query, read result.
- Agent heartbeat management: `mkdir` to register, `touch keepalive` to heartbeat, auto-removal on timeout.
- Cross-FS operations: `cp local:/tmp/data.txt /s3fs/mybucket/` copies local file to S3.
- Custom shell (`agfs-shell`) with scripting support (`.as` files).
- FUSE support on Linux for native filesystem mounting.
- HTTP API for non-FUSE access.

**Relevance to AI-VFS:** AGFS is the most philosophically aligned project for an AI virtual filesystem.
Its key insight -- that AI agents understand file operations natively (`cat`, `echo`, `ls`) so all services should be exposed as files -- is the foundational thesis.
The composition of multiple virtual filesystems (KV, queue, SQL, S3) into a unified namespace is exactly the pattern needed.
The agent-specific features (heartbeat management, task queue as filesystem operations, cross-FS copy) show practical patterns for multi-agent coordination.
The main gap is that AGFS is a server-side system with its own shell, not an embeddable library -- an AI-VFS would want to combine AGFS's unified-namespace philosophy with bashkit/just-bash's in-process execution model.

---

## Synthesis: Architectural Patterns for an AI Virtual Filesystem

The surveyed projects cluster into three layers:

### Filesystem Abstraction Layer

- **fsspec**: Python-native interface standard (implement this for ecosystem compatibility)
- **filestash**: Minimal backend interface (8 methods) that unifies 20+ storage protocols
- **JuiceFS**: Metadata/data separation pattern for scalable distributed filesystems

### Virtual Environment Layer

- **just-bash / bashkit**: In-process virtual bash with composable VFS (InMemory, Overlay, Mountable)
- **TigerFS**: Database-backed filesystem with ACID transactions and version history
- **AGFS**: Unified namespace over heterogeneous backends (KV, queues, databases, object storage)

### Sandboxed Execution Layer

- **Monty**: From-scratch Python VM in Rust, sub-microsecond startup, snapshotting, external-function-only I/O
- **Eryx**: Full CPython in Wasm sandbox, async callbacks, sandbox pooling, native extensions
- **Starlark-pyo3**: Deterministic configuration language evaluation (niche use)

A complete AI virtual filesystem would likely combine:

1. fsspec-compatible interface for Python ecosystem integration
2. Composable VFS layers (in-memory, overlay, database-backed) from just-bash/bashkit
3. AGFS-style unified namespace for exposing heterogeneous services as files
4. TigerFS-style database backing for ACID guarantees and version history
5. Monty or eryx as the sandboxed execution engine, with VFS operations exposed as external functions
6. Filestash-style plugin architecture for extensible backends
