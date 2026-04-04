# In-process sandboxed execution environments for multi-tenant code

**WASM-based isolation via wasmtime is the only approach that blocks all major attack vectors without OS-level primitives.**
The Arize Phoenix team empirically tested six security vectors across every major sandbox backend and found that CPython compiled to WASM (running inside wasmtime) was the sole no-sidecar option passing all six — memory isolation, env-var exfiltration, outbound networking, filesystem reads, subprocess spawning, and CPU/memory exhaustion.
Every CPython core developer agrees: you cannot securely sandbox CPython in-process at the Python level.
The real question is which interpreter VM or WASM runtime to wrap around code execution, and the answer depends on your latency/compatibility tradeoffs.

This report covers **34 tools and approaches** across six categories, evaluated for a multi-tenant web app running LLM-generated code in-process.

---

## WASM runtimes are the strongest in-process isolation primitive

The defining advantage of WASM sandboxing is **hardware-enforced linear memory isolation** — each WASM instance gets its own memory space that physically cannot address host memory.
Combined with WASI's capability-based model (zero filesystem/network access unless explicitly granted), this creates a sandbox that doesn't depend on blocklists, AST rewriting, or environment stripping.

**wasmtime-py** (v41.0.0, January 2026) is the clear winner among WASM runtimes with Python bindings.
Maintained monthly by the Bytecode Alliance, it exposes fuel-based CPU metering (`store.add_fuel(400_000_000)`), epoch-based interruption for deadline timeouts, memory limits via `store.set_limits()`, and WASI preopen directories for capability-based filesystem control.
Each `Store` object is a strict isolation boundary — WASM objects from different stores cannot interact.
You share one compiled `Module` (CPython.wasm) across all tenants and create a fresh `Store` + `WasiConfig` per execution.
The proven pattern, documented by Simon Willison, loads VMware's pre-built `python-3.11.1.wasm` binary with zero preopened directories, stdout/stderr captured to files, and a fuel budget that traps on exhaustion.

**wasmer-py** is effectively dead — last released in January 2022, incompatible with Python 3.11+, and the Wasmer company has pivoted to their cloud/edge platform.
**WAMR** (WebAssembly Micro Runtime) has Python bindings in its repository but they're experimental, undocumented, and not on PyPI.
**Extism** offers a higher-level plugin abstraction over Wasmtime with a clean Python SDK (`pip install extism`), per-plugin memory limits, and a host-function decorator pattern, but it lacks direct fuel metering exposure and requires plugins to conform to its specific ABI.

The cold-start problem for CPython-in-WASM is real — **~650ms to compile** the CPython module on first load, reducible to ~16ms with ahead-of-time compilation.
This is where Eryx enters.

## Eryx is purpose-built for this exact use case

**Eryx** (github.com/eryx-org/eryx, 41 stars, Apache-2.0/MIT) is a Rust library by Ben Sully that wraps wasmtime + CPython 3.14 WASI into a complete sandboxed Python execution system.
It launched in early 2026 and directly targets multi-tenant LLM code execution.
Available as a Rust crate, Python package (`pyeryx` on PyPI), and npm package.

Its key innovations over raw wasmtime-py solve the operational pain points.
**Sandbox pooling** (`SandboxPool`) maintains warm instances with pre-warming, bounded concurrency, idle eviction, and statistics tracking.
**Pre-initialization** captures Python's initialized memory state at build time, yielding **~25x faster sandbox creation** (450ms → 18ms).
**Pre-compiled Wasm** via AOT gives **41x faster creation** (650ms → 16ms).
Per-execution overhead drops to **~1.6ms** with pre-compilation.

Eryx also provides session state persistence (variables, functions, and classes persist between executions for REPL-style usage), state snapshots via pickle serialization, execution tracing via `sys.settrace`, cancellation via epoch interruption, host-controlled TCP/TLS networking with allowlists, virtual filesystem mounting, secret scrubbing, and MCP server support.
Experimental native extension support means numpy compiled to WASI can work.
The project is young (413 commits, 4 contributors including AI assistance) but architecturally the most complete solution for this specific problem.

## Pyodide requires Deno — it is not a sandbox by itself

CVE-2025-68668 (CVSS 9.9) proved definitively that **Pyodide alone provides no security boundary on a server**.
Researchers from Cyera Labs demonstrated escaping Pyodide's sandbox in n8n via `_pyodide._base.eval_code()` and ctypes-like indirection to invoke `system()` without touching `os.system`.
Grist-Core had a similar escape (CVSS 9.1), fixed by moving Pyodide execution into Deno.
Pyodide is a CPython port to WebAssembly via Emscripten — it requires a JavaScript runtime (V8/SpiderMonkey) and cannot run inside wasmtime.

The **Pyodide + Deno** pattern works: Deno's `--deny-read --deny-write --deny-net --deny-env` permission flags restrict what the process can do, and Pyodide's Emscripten MEMFS provides an ephemeral in-memory filesystem.
This is the approach used by LangChain's `langchain-sandbox` package, Pydantic's `mcp-run-python`, and Hugging Face smolagents' `WasmExecutor` (introduced in v1.20.0).
Cloudflare Workers runs Pyodide inside V8 isolates for their Python Workers product.
However, this spawns a **Deno subprocess per execution** — it's not truly in-process, and it lacks wasmtime's fuel metering (relying instead on timeout-based process killing).
Pyodide's major advantage is ecosystem breadth: full CPython 3.13 with NumPy, Pandas, scikit-learn, and many C-extension packages pre-ported.

**RustPython** compiles to WASM/WASI (`cargo build --target wasm32-wasip1`) and runs inside wasmtime with full capability-based isolation, but Python compatibility is incomplete — "in development, not totally production-ready" per its README.
**MicroPython** compiled to WASM is too limited (missing most stdlib modules) for general LLM-generated code.
**componentize-py** (Bytecode Alliance) compiles Python into WASM Component Model components but is a build-time tool, not a runtime sandbox — though Eryx uses its CPython WASI build internally.

## V8 isolates from Python via PyMiniRacer and QuickJS

For JavaScript execution, two options stand out with excellent Python APIs.

**PyMiniRacer** (`pip install mini-racer`, github.com/bpcreech/PyMiniRacer) wraps V8 14.4 with one isolate per `MiniRacer()` instance.
Complete memory isolation between instances, V8 sandbox enabled on all platforms, **hard heap memory limits and eval timeouts** built in.
The API is clean: `ctx.eval("code")`, `ctx.call("fn", args)`, `wrap_py_function()` for callbacks.
Each isolate starts at **~1.7MB** physical memory with ~37MB shared library.
Hundreds of isolates per process are practical.
Originally by Sqreen (2016), revived and actively maintained by Ben Creech since March 2024 with regular V8 updates and pre-built wheels for all major platforms.
No filesystem or network access exists by default — V8 is a pure computation engine.
The main limitation: JavaScript only, no TypeScript compilation, no Node.js APIs.

**QuickJS** via the `quickjs` PyPI package (by PetterS) provides an even lighter alternative — the entire runtime is **~600KB** with built-in `set_memory_limit(bytes)` and `set_time_limit(seconds)`.
Zero host system access unless explicitly injected.
Thread-safe `Function` class.
Multiple `Context` instances per process, each fully isolated.
The package was last updated November 2023, which is a maintenance concern, but the underlying engine is solid (Fabrice Bellard).
**QuickJS-NG** (github.com/quickjs-ng/quickjs, 2,800 stars) is the actively maintained community fork with 16 releases through February 2026, tracking the latest ECMAScript specification.
Rust bindings exist via `quickjs-rusty`; a dedicated Python binding for QuickJS-NG is emerging but not yet mature on PyPI.

Both V8 isolates and QuickJS carry a critical caveat from every major vendor: **V8 isolates alone are not sufficient for adversarial multi-tenant without additional OS-level defenses**.
V8 has had numerous sandbox escape CVEs.
Cloudflare patches within hours of V8 security releases and layers process isolation, kernel features, and rapid patching on top of workerd's V8 isolates.
The open-source workerd README explicitly warns it "does NOT contain suitable defense-in-depth against implementation bugs."

**deno_core** (the Rust crate powering Deno) can be embedded in Python via PyO3 — VlConvert successfully did this — but no pre-built Python package exists.
**STPyV8** offers deep Python↔JS interop but the large attack surface makes it unsuitable for sandboxing.
**Cloudflare workerd** is a standalone binary, not an embeddable library.
**Supabase Edge Runtime** is the closest thing to self-hosted Deno Subhosting, running as a Docker container with dual-runtime isolation (trusted main runtime + restricted user runtime).

## Language-specific VMs offer different tradeoff profiles

**Starlark** (Google's sandboxed Python dialect for Bazel) provides the **strongest isolation guarantee of any option** — hermetic execution by language design.
No filesystem access, no network, no system clock, no randomness, deterministic evaluation.
No while loops (prevents infinite loops structurally), no recursion beyond configurable limits, no classes or exceptions.
The `starlark-pyo3` package (github.com/inducer/starlark-pyo3) wraps the Rust implementation via PyO3 with binary wheels for major platforms.
LLMs can generate Starlark easily since it's syntactically Python.
The tradeoff is expressiveness: no general-purpose algorithms, no OOP, limited stdlib.
Excellent for data transformation, policy evaluation, and configuration — not for complex code.

**Lua via lupa** (`pip install lupa`, v2.6) embeds Lua 5.4 or LuaJIT in Python with separate `LuaRuntime` instances per tenant (~600-800KB each).
Isolation requires manual environment whitelisting — remove `io`, `os`, `debug`, `package` libraries and provide only safe functions.
Lua 5.4's `debug.sethook` enables instruction-count limits for infinite loop prevention, but **LuaJIT does not support this** — use PUC-Rio Lua 5.4 only for sandboxing.
The `sandbox.lua` library (kikito/lua-sandbox) provides a ready-made whitelist with 500,000-instruction default quota.
Mozilla's `lua_sandbox` is a C library focused on telemetry/data pipelines with no Python bindings and low recent maintenance activity.
**Luau** (Roblox's Lua 5.1 derivative) has built-in sandboxing used for millions of untrusted scripts but lacks Python bindings.

**CEL** (Common Expression Language) is inherently safe — non-Turing-complete, side-effect free, guaranteed termination.
The `common-expression-language` package (v0.5.6, February 2026) wraps a Rust core via PyO3 with microsecond evaluation.
Google just announced `cel-expr-python` (March 2026) wrapping the official C++ implementation.
CEL excels for policy evaluation, data filtering, and validation rules but cannot express loops or function definitions.
**Jsonnet** is Turing-complete but output-only (JSON), with a dangerous `import` mechanism that reads arbitrary files unless restricted via custom `import_callback`.
**Rhai** (Rust scripting language, ~5k stars) has excellent sandbox design with configurable memory/CPU limits but no Python bindings.
**Boa** (Rust JS engine), **Tengo** (Go scripting), and **mRuby** all lack Python bindings.
**Tcl's safe interpreter** (`interp create -safe`) provides a historically interesting dual-interpreter sandboxing model accessible via Python's `tkinter`, but the integration is awkward, LLMs don't generate Tcl, and there are no resource limits.

## Python-level sandboxing is fundamentally broken

Every CPython core developer who has commented — Victor Stinner (pysandbox author), Alyssa Coghlan, Brett Cannon — agrees: **"Run the Python process in a sandbox, don't run a sandbox in Python."**
The language's introspection capabilities make escape from any Python-level sandbox structurally inevitable.

**PEP 578 audit hooks** (`sys.addaudithook()`) are explicitly not a sandbox — the PEP's own "Why Not A Sandbox" section states this.
Hooks are advisory; malicious code with ctypes or C-extension access bypasses them.
Object introspection chains (`__subclasses__()`) reach dangerous classes without triggering audited operations.
Useful only as a monitoring/logging layer.

**Python subinterpreters** (PEP 684/734, Python 3.12-3.14) provide per-interpreter GILs for concurrency but zero security isolation.
Brett Cannon's recommendation: "If you want Python in a sandbox you're probably best using the WASI build of CPython."
Import restrictions via `sys.meta_path` hooks are trivially bypassed through `__subclasses__()` chains, `importlib.import_module()`, or traversing objects already in memory.
**RestrictedPython** (Zope/Plone) acknowledges in its own documentation that it "is not a sandbox system or a secured environment."
**PyO3** embeds the full CPython interpreter — a GitHub discussion (#2080) explicitly states "pyo3 is not meant for sandboxing."
**PyPy Sandbox** had an excellent architectural design (all syscalls marshaled through stdout to an external controller) but is effectively unmaintained and removed from PyPy mainline.

The only Python-level approach with any novelty is **sandboxed-python** (`pip install sandboxed-python`), which implements "Finite Python" (FPy) — a restricted subset with no loops, no recursion, using AST analysis and allowlists.
Designed specifically for LLM tool calls.
Too restrictive for general code execution but conceptually interesting as a fast pre-screening layer.

## The LLM code execution ecosystem in 2026

The market has bifurcated into **cloud sidecar services** and **in-process interpreters**, with little middle ground.

**E2B** (e2b.dev) is the dominant cloud sandbox — Firecracker microVMs with ~150ms creation time, used by 88% of Fortune 100 (per their marketing), integrated into Hugging Face smolagents, LangChain, and the Manus agent platform.
Full Linux environment, any language, any library, ~50,000 concurrent sessions.
Not in-process.
**Modal** offers similar container-based sandboxes with pay-per-CPU-cycle pricing and sub-second spinup.
**llm-sandbox** (vndee/llm-sandbox) wraps Docker/Podman/Kubernetes.
All of these require OS-level isolation — they don't fit the in-process constraint.

For in-process, the landscape is:

- **Eryx** — The most complete purpose-built solution.
  CPython 3.14 in WASM with pooling, pre-init, fuel metering, virtual FS, networking policies, session state, and MCP support.
  Young but architecturally excellent.
- **Pydantic Monty** — Rust Python-subset interpreter with **3-15µs cold start** (10,000× faster than subprocess). ~50% stdlib coverage, no classes yet, shared heap (no hardware memory isolation).
  Best used as a fast-path with fallback.
- **CPython-in-wasmtime** (raw) — Full CPython stdlib, all six security vectors blocked, but ~300-500ms cold start and no C-extension packages.

The **Arize Phoenix evaluation** (February 2026, github.com/Arize-ai/phoenix issue #11756) provides the most rigorous public comparison.
Their recommended architecture: Monty as fast-path (3-15µs) with automatic fallback to CPython-in-WASM (wasmtime) on `ModuleNotFoundError`, and E2B sidecar for anything requiring pip packages.
An 8-thread wasmtime pool achieves **130-198 executions/second** at ~30ms p50 latency.

## Recommended architecture and tool selection

The optimal design for a multi-tenant web app executing LLM-generated code uses a **tiered execution model** with three layers, selected per-request based on code complexity analysis:

**Tier 1 — Expression evaluation (sub-millisecond).**
Use **Starlark** (`starlark-pyo3`) or **CEL** (`common-expression-language`) for simple expressions, data filtering, and policy evaluation.
Guaranteed termination, zero I/O, hermetic by design.
Route here when AST analysis shows no imports, no loops, no function definitions beyond simple expressions.

**Tier 2 — In-process interpreted execution (microseconds to milliseconds).**
Use **Pydantic Monty** for Python-subset code or **QuickJS** for JavaScript.
Monty's 3-15µs latency makes it ideal for high-volume simple computations.
QuickJS's built-in memory/time limits and zero-I/O default make it the strongest JS option.
Neither provides hardware memory isolation — treat as a fast path, not a security boundary for adversarial code.

**Tier 3 — WASM-sandboxed full Python (tens of milliseconds).**
Use **Eryx** (preferred, for its pooling/pre-init/session management) or raw **wasmtime-py** + CPython.wasm.
This is the only in-process option blocking all six security vectors.
Fuel metering prevents infinite loops, WASI preopens control filesystem access, and linear memory isolation provides a hardware-enforced boundary.
Accept the stdlib-only limitation or invest in Eryx's experimental native extension support.

**Fallback — Cloud sidecar (hundreds of milliseconds).**
For code requiring pip packages (NumPy, Pandas, requests), route to **E2B** or a self-hosted Firecracker/Docker sidecar.
No in-process solution supports arbitrary pip dependencies securely.

For **JavaScript-only tenants** in a Python web app, **PyMiniRacer** (`pip install mini-racer`) is the simplest path — one V8 isolate per tenant, memory limits, timeouts, no filesystem, installed via pip with pre-built wheels.
For a full JS/TS platform with dynamic code loading, **Supabase Edge Runtime** (self-hosted Docker) provides the closest thing to self-hosted Deno Subhosting.

## Conclusion

The landscape has matured significantly since 2024.
Three developments changed the calculus: CVE-2025-68668 eliminated naive server-side Pyodide as an option, Eryx emerged as a purpose-built CPython-in-WASM sandbox with production-grade pooling, and the Arize Phoenix evaluation provided empirical security data that validates WASM as the only in-process approach passing all attack vectors.
The fundamental insight remains that **isolation must come from the runtime boundary (WASM linear memory, V8 isolate), not from language-level restrictions** — every attempt to sandbox Python at the Python level has failed and will continue to fail due to the language's introspection capabilities.
The viable path forward combines a fast in-process interpreter (Monty/QuickJS) for the common case with WASM-sandboxed CPython (Eryx/wasmtime) for the security-critical path, accepting that C-extension packages still require OS-level isolation via a sidecar service.
