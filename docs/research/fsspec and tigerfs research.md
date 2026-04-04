# How fsspec and TigerFS connect to in-process code sandboxes

**No existing project directly bridges fsspec to any of these sandboxes**, but every sandbox studied offers a clean extensibility mechanism — a trait, interface, or function-injection API — that makes integration architecturally straightforward.
The core insight is that these sandboxes fall into three distinct integration patterns: **function injection** (Monty, Starlark, PyMiniRacer, QuickJS), **filesystem trait implementation** (Bashkit, just-bash), and **WASI capability mapping** (Eryx, wasmtime-py).
TigerFS, meanwhile, is a very new FUSE-only product with no programmatic API, making it a blunt instrument for this use case.
The most practical near-term approach for every sandbox is a **stage-in / execute / stage-out** pattern using fsspec, with live cloud-backed filesystems requiring Rust or TypeScript work at the trait level.

---

## The three integration patterns across eight sandboxes

Each sandbox's isolation philosophy dictates how a filesystem layer can reach inside it.
The sandboxes cluster neatly into three groups based on their integration surface.

**Function injection sandboxes** (Monty, Starlark, PyMiniRacer, QuickJS) have zero I/O by default and expose a mechanism to register host-side callable functions as globals inside the sandbox.
The host wraps fsspec operations (read, write, list, exists) as simple functions and injects them.
Sandboxed code calls these functions by name.
This is the simplest pattern — **pure Python, no Rust required** — and works today with each runtime's existing API.

**Filesystem trait sandboxes** (Bashkit, just-bash) define a formal filesystem interface (a Rust trait or TypeScript interface) that their shell interpreter uses for all file operations.
The built-in implementations are in-memory or overlay-based.
Cloud-backing requires implementing the trait against a cloud SDK (`object_store` in Rust, `@aws-sdk/client-s3` in TypeScript).
This is the most architecturally elegant pattern but requires writing code in the sandbox's native language.

**WASI capability sandboxes** (Eryx, wasmtime-py with CPython WASM) use WebAssembly's preopened-directory model, where the host grants the guest specific directory handles.
The Python-level API (`WasiConfig.preopen_dir()`) **only accepts real OS paths** — there is no way from Python to provide a virtual filesystem.
Bridging to cloud storage requires either staging files to a temp directory, mounting via FUSE, or implementing custom `WasiDir`/`WasiFile` traits in Rust.

---

## Function injection: Monty, Starlark, PyMiniRacer, QuickJS

### Pydantic Monty — the cleanest integration

Monty (pydantic/monty, **6.4k stars**, Rust-based Python subset VM) has the most thoughtful external function system of any sandbox studied.
Functions are declared by name at parse time and provided as callables at runtime.
Monty supports **bytes natively**, async external functions via `run_monty_async`, and a unique **snapshot/resume** mechanism where execution pauses at an external function call, serializes state, and can resume later with the result.

The fsspec bridge is trivial Python:

```python
fs = fsspec.filesystem("s3", key="...", secret="...")
fs_funcs = {
    "file_read": lambda path: fs.cat(path).decode("utf-8"),
    "file_write": lambda path, data: fs.pipe(path, data.encode("utf-8")),
    "file_list": lambda path: fs.ls(path, detail=False),
}
m = pydantic_monty.Monty(code, external_functions=list(fs_funcs.keys()))
result = m.run(external_functions=fs_funcs)
```

The snapshot capability is particularly valuable for multi-tenant scenarios: a sandbox can pause mid-execution when it needs a file, serialize its state, and resume when the I/O completes — enabling **non-blocking, event-driven execution** across many tenants.
One limitation: Monty cannot handle file-like objects across the boundary, so all reads/writes must use whole-file operations (`cat`/`pipe`) rather than streaming.

### Starlark (starlark-pyo3) — JSON conversion bottleneck

starlark-pyo3 (inducer/starlark-pyo3, **36 stars**) exposes `module.add_callable(name, callable)` for injecting Python functions.
The critical limitation is that **all values pass through JSON serialization** as an intermediate format.
This means no bytes type — binary data must be base64-encoded — and large payloads incur serialization overhead.
Starlark also has **no exception handling** (no try/except), so filesystem errors terminate evaluation entirely.
Error-resilient code must check `file_exists()` before reading.

The integration is Python-only and straightforward, but Starlark's restrictions (no classes, no mutation after freeze, deterministic by design) make it better suited as a **configuration/orchestration language** than a general-purpose scripting environment for file manipulation.

### PyMiniRacer — async-only callbacks with binary support

PyMiniRacer (bpcreech/PyMiniRacer, package `mini-racer` on PyPI, **actively maintained**) embeds V8 in Python.
Its `wrap_py_function()` API injects Python functions as **async JavaScript functions** returning Promises — V8's single-threaded architecture means synchronous callbacks would deadlock the isolate.
All injected filesystem functions must be awaited on the JS side.

PyMiniRacer's standout advantage is **native bytes-to-ArrayBuffer conversion**, making it the only JS sandbox that can handle binary files without base64 encoding.
Memory limits (`set_hard_memory_limit`) and execution timeouts provide resource control.
The async model also aligns well with fsspec's `AsyncFileSystem` implementations for S3, GCS, and Azure.

### QuickJS — simpler but archived

The primary Python binding (`quickjs` on PyPI) offers synchronous `add_callable(name, func)` — simpler than PyMiniRacer's async model.
However, the **package was archived in January 2026** and is no longer maintained.
It supports only string and numeric types (no bytes), and its thread-hostile `Context` class complicates multi-tenant deployments.
For new projects, PyMiniRacer is the stronger choice despite its larger footprint (~37MB vs ~2MB).

---

## Filesystem trait implementation: Bashkit and just-bash

### Bashkit — Rust trait ready for cloud backends

Bashkit (everruns/bashkit, crates.io `bashkit` v0.1.4, **Rust**) is an async virtual bash interpreter explicitly designed for multi-tenant environments.
It exports a `FileSystem` trait used as `Arc<dyn FileSystem>`, with three built-in implementations: `InMemoryFs`, `OverlayFs` (copy-on-write), and `MountableFs` (union mounts at specific paths).
The `MountableFs` architecture is purpose-built for mounting different backends at different paths.

Cloud integration requires **implementing the `FileSystem` trait in Rust**, most naturally backed by the `object_store` crate (Apache Arrow's cloud storage abstraction supporting S3, Azure Blob, and GCS) or Apache OpenDAL (40+ storage backends).
The trait methods map cleanly: `read_file` → `object_store::get().bytes()`, `write_file` → `object_store::put()`, `read_dir` → `object_store::list_with_delimiter()`.
Bashkit's tokio-based async architecture aligns perfectly with these async Rust storage crates.

Bashkit does have **Python bindings** via PyO3 (a `BashTool` class with LangChain integration), but these don't expose the filesystem trait to Python.
A Python-side stage-in/stage-out pattern works today; live cloud access requires Rust.

### just-bash — TypeScript interface with MountableFs

just-bash (vercel-labs/just-bash, npm `just-bash` v2.12.8, **TypeScript**, ~2k stars) has the same architectural pattern as Bashkit — an `IFileSystem` interface with `InMemoryFs`, `OverlayFs`, `MountableFs`, and `ReadWriteFs` implementations.
It was explicitly designed for AI agents (built by Vercel Labs with contributors including Malte Ubl).

Cloud-backing would mean implementing `IFileSystem` in TypeScript against `@aws-sdk/client-s3` or similar.
The `MountableFs` class already supports dynamic `.mount()` calls, so a cloud-backed filesystem could be mounted at `/cloud` while keeping `/tmp` as `InMemoryFs`.
The simplest immediate approach is staging: download files as a `Record<string, string>` dictionary, pass it as `new Bash({ files })`, execute, then upload results.

**Key limitation**: `InMemoryFs` is constrained by Node.js heap size (~1.5GB default), and `OverlayFs`/`ReadWriteFs` have a **10MB max file read** default.
Large datasets would need streaming support that doesn't currently exist.

---

## WASI sandboxes: Eryx and wasmtime-py

### How WASI preopened directories actually work

WASI uses a **capability-based security model** where WASM modules start with zero filesystem access.
The host grants access by "preopening" specific directories and providing file descriptor handles to the guest.
When sandboxed code calls `open()`, WASI libc transparently routes the path through preopened directories.
Paths outside granted directories are denied.

**The fundamental constraint**: wasmtime-py's `WasiConfig.preopen_dir(host_path, guest_path)` takes only **real OS filesystem paths**.
There is no Python-level mechanism to substitute a virtual or in-memory filesystem.
This is a thin ctypes wrapper over the C API, and the lower-level Rust `WasiDir`/`WasiFile` trait system is not exposed to Python.

### Eryx — the most capable WASM sandbox studied

Eryx (eryx-org/eryx, **41 stars**, Rust) runs **CPython 3.14 compiled to WASI** inside Wasmtime.
It achieves **~16ms sandbox creation** with pre-compiled WASM (41x faster than cold start) and supports session state persistence, execution tracing, cancellation via epoch interruption, and a `SandboxPool` for managing warm instances.
It has Python bindings on PyPI as `pyeryx`.

Eryx's primary interaction model is **callback-based**: the Rust host defines typed async callbacks that Python code in the sandbox calls via `await`.
Rather than giving sandboxed Python direct filesystem access, the host provides specific operations.
This means two integration paths exist:

**Callback-based (Eryx-native)**: Implement filesystem operations as `TypedCallback` traits.
Sandboxed code calls `content = await read_file("/data/file.txt")` which the host maps to `fsspec.open("s3://bucket/file.txt")`.
This is clean and explicit but means sandboxed code must use custom functions rather than standard `open()`.

**WASI-level (Wasmtime-native)**: Implement custom `WasiDir`/`WasiFile` traits in Rust, backed by cloud storage.
Standard Python `open()` and `os.listdir()` inside the sandbox transparently route through the custom backend.
This is transparent to sandboxed code but requires significant Rust work and complex async bridging.

### Three practical approaches for WASI sandboxes

**Staging pattern** (works today, Python-only): Use fsspec to download files to a `tempfile.TemporaryDirectory()`, call `preopen_dir(tmpdir, "/data")`, run the WASM module, then sync modified files back.
Simple and reliable but requires full download before execution and temp disk space.

**FUSE mount pattern** (works today, requires FUSE support): fsspec includes an experimental `fsspec.fuse.run()` that can mount **any fsspec filesystem** as a FUSE mount.
Alternatively, `s3fs-fuse` provides a mature S3 FUSE mount.
Point `preopen_dir()` at the mount point for transparent lazy-loading cloud access.
Requires FUSE kernel support — unavailable in many container environments.

**Custom Rust `WasiDir`/`WasiFile`** (most powerful, significant effort): wasmtime's `wasi-common` crate explicitly documents this extensibility point: _"This separation of concerns makes it pretty enjoyable to write alternative implementations, e.g. a virtual filesystem."_
You implement the `WasiDir` trait (readdir, open_file, create_dir, etc.) and `WasiFile` trait (read, write, seek, stat) backed by any data source.
This is architecturally correct but cannot be driven from Python — it requires building a custom Rust crate or extending wasmtime-py.

---

## TigerFS is FUSE-only and very early-stage

TigerFS (tigerfs.io) is a **Postgres-backed virtual filesystem** built by Tiger Data (the company formerly known as Timescale, creators of TimescaleDB).
It mounts a PostgreSQL database as a directory where every file is a database row and every write is a transaction.
The architecture has four layers: Unix tools → FUSE/NFS → TigerFS daemon → PostgreSQL.

**What TigerFS is not**: It has **no Python API**, no PyPI package, no in-process library mode, and no public GitHub repository (the linked `github.com/timescale/tigerfs` appears to be private).
The entire API surface is the filesystem itself — you interact via `ls`, `cat`, `echo >`, etc. on a mount point.
The product appears to be in **early access or beta** given the sparse documentation and absence of third-party coverage.

For sandbox integration, TigerFS's only mechanism is the **mount-then-point** pattern: mount TigerFS via FUSE, then direct the sandbox's filesystem layer at the mount point.
For WASI sandboxes, this means `preopen_dir("/mnt/tigerfs", "/data")`.
For Bashkit/just-bash, the sandbox would need to use a host-filesystem-backed implementation pointing at the mount.

**Key limitations for multi-tenant use**: No documented multi-tenant isolation beyond standard Postgres permissions.
No direct S3/cloud object storage support — the backend is exclusively PostgreSQL.
FUSE requirement makes it unsuitable for containerized environments without privileged access.
No performance benchmarks published.
**Important disambiguation**: TigerFS (Tiger Data/Timescale) is entirely unrelated to TigrisFS (Tigris Data), which is an S3-compatible FUSE adapter.

---

## WASI virtual filesystem implementations compared

| Implementation                   | Runtime               | Read/Write | Dynamic backend             | Python-accessible | Maturity                                   |
| -------------------------------- | --------------------- | ---------- | --------------------------- | ----------------- | ------------------------------------------ |
| **wasi-common WasiDir/WasiFile** | Wasmtime              | Read-write | Yes (custom Rust impl)      | Rust only         | Mature, designed for this                  |
| **wasi-vfs**                     | Any WASI runtime      | Read-only  | No (build-time embed)       | CLI tool          | Stable (v0.5.5)                            |
| **WASI-Virt**                    | Wasmtime (components) | Read-only  | No (post-compile)           | CLI tool          | Early (154 stars)                          |
| **Wasmer virtual-fs**            | Wasmer only           | Read-write | Yes (custom Rust impl)      | No                | Active (v0.601.0)                          |
| **cap-std**                      | Wasmtime (underlying) | Read-write | No (real OS only)           | No                | Mature                                     |
| **tmpdir staging**               | Any                   | Read-write | Yes (via fsspec)            | Python            | Production-ready                           |
| **FUSE mount**                   | Any                   | Read-write | Yes (s3fs-fuse/fsspec.fuse) | Python            | s3fs-fuse mature; fsspec.fuse experimental |

The **wasmtime `wasi-common` WasiDir/WasiFile traits** are the canonical extensibility point for custom WASI filesystems.
The crate explicitly provides no filesystem implementation of its own — only `ReadPipe` and `WritePipe` for virtual streams.
Embedders must supply their own via `wasi-cap-std-sync` (real OS filesystem) or custom implementations.
Wasmer's `virtual-fs` provides an excellent reference architecture (in-memory FS, overlay FS, chained FS) but is not compatible with wasmtime.

An open wasmtime issue (#8963, "How to get control over filesystem access with `wasmtime_wasi::WasiCtxBuilder`") confirms that **the newer WASIp2 path lacks clear documentation** for custom filesystem providers, despite the legacy `wasi-common` path being well-designed for it.

---

## Existing projects that solve adjacent problems

The closest existing projects to a complete fsspec-to-sandbox integration are:

**AgentFS** (Turso/tursodatabase) is a SQLite-backed virtual filesystem purpose-built for AI agents.
It stores files, key-value pairs, and audit trails in a single `.db` file.
It supports FUSE mounting, copy-on-write overlays via Linux mount namespaces, and even runs in browsers via WASM.
AgentFS represents the **most directly relevant architectural pattern** — virtual FS → sandbox → code execution — though it uses its own SDK rather than fsspec.

**Localsandbox** (CoPlane) combines AgentFS + just-bash + Pyodide into a single local agent sandboxing solution.
This is the **closest existing project** to the architecture described in the query: a virtual filesystem (SQLite) connected to both a bash interpreter (just-bash) and a Python WASM sandbox (Pyodide).
Files written inside sandboxes go to SQLite, not the real filesystem.

**llm-wasm-sandbox** (PyPI package) is a production-grade WASM sandbox for executing untrusted Python/JavaScript from LLMs, with a **pluggable storage adapter interface** and UUID-based per-session workspace isolation.
Its architecture mirrors the desired fsspec integration pattern.

Among cloud sandbox providers, **E2B** (Firecracker microVMs, used by 88% of Fortune 100), **Modal** (gVisor, with sophisticated filesystem snapshots), **Daytona** (Docker/OCI containers, **$24M Series A**), and **Fly.io Sprites** (persistent Firecracker VMs with 100GB NVMe + object storage) all solve the sandbox-with-filesystem problem but at the VM/container level rather than the in-process level.

---

## What to build: a practical integration roadmap

**Immediate (Python-only, works today)**: For Monty, Starlark, PyMiniRacer, and QuickJS, build a thin `FsspecBridge` class that wraps an fsspec filesystem instance and exposes `read(path) → str`, `write(path, str)`, `list(path) → list`, `exists(path) → bool` as injectable functions.
This is **~50 lines of Python** per sandbox, plus path validation for security.
For WASI sandboxes (Eryx, wasmtime-py), use the staging pattern: fsspec downloads to a temp directory, preopen that directory, execute, sync back.

**Medium-term (Rust/TypeScript, enables live cloud access)**: For Bashkit, implement the `FileSystem` trait backed by the `object_store` crate.
For just-bash, implement `IFileSystem` backed by `@aws-sdk/client-s3`.
For Eryx, implement filesystem operations as `TypedCallback` traits bridging to fsspec or `object_store`.
These integrations let sandboxed code access cloud storage transparently through native file operations.

**Long-term (maximum capability)**: Implement custom `WasiDir`/`WasiFile` traits in Rust backed by cloud storage, with a caching layer for latency reduction.
Extend wasmtime-py via PyO3 to expose this virtual filesystem system to Python.
This would give **any WASI-based sandbox** (Eryx, raw wasmtime-py, future WASI runtimes) transparent, live cloud-storage-backed filesystems — the architecturally correct solution.

## Conclusion

The landscape splits cleanly: **function-injection sandboxes are trivially integrated via Python today**, filesystem-trait sandboxes need modest native-language work to implement their trait against cloud SDKs, and WASI sandboxes face a genuine gap where the Python bindings don't expose the virtual filesystem extensibility that the Rust layer explicitly supports.
TigerFS is too early-stage and FUSE-dependent to serve as a general solution.
The most promising near-term architecture borrows from Localsandbox and AgentFS: use fsspec's `MemoryFileSystem` or `DirFileSystem` as the Python-side abstraction, stage files into whichever sandbox technology is in use, and sync results back.
For production multi-tenant deployments, the combination of Eryx's callback system with fsspec's cloud backends offers the best balance of isolation, flexibility, and Python-level control.
