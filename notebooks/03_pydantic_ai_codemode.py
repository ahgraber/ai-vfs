# %% [markdown]
# # ai-vfs — Code-mode tools for a pydantic-ai agent
#
# An agent that works with files needs a place to *run code* against those files
# that you normally have to build yourself. ai-vfs gives that over a **governed**
# store — every read, write, and sandbox call is checked against the calling
# principal's permissions and audited — and a consumer reaches all of it through
# the public `vfs` package alone, never a `vfs.*` submodule. This notebook wires
# that capability onto a [pydantic-ai](https://ai.pydantic.dev) agent as a tool.
#
# A few terms used throughout:
# - **code mode** — instead of one tool per operation, give the agent a sandbox
#   and let it *write code* that calls the filesystem; `vfs.execute` runs it.
# - **sandbox mount** — the sandbox sees the governed VFS as its filesystem, not
#   the host's; it can only touch what the principal may. Native I/O inside the
#   sandbox (`open(path).read()`, `open(path, 'w').write(...)`) routes through it.
# It deliberately is **not** a shipped pydantic-ai integration — it is the wiring,
# small enough to read, so you can lift the pattern into your own agent.
#
# Step through top to bottom in **VS Code Interactive** or **Jupyter** — the kernel
# owns the event loop, so top-level `await` works in a cell (do not call
# `asyncio.run()`).  Edit any input and re-run a cell to explore — git holds the original.
# Cells are safe to re-run unless marked `# not idempotent` (those bump a version each run;
# Restart→Run-All resets). The sandbox cells need `pydantic-monty` and the agent cells need
# `pydantic-ai`, both in the default `uv sync` dev group.

# %%
from __future__ import annotations

import importlib.util
import shutil
import tempfile

from vfs import VFS, ResourceLimits, VFSConfig

HAS_MONTY: bool = importlib.util.find_spec("pydantic_monty") is not None
HAS_PYDANTIC_AI: bool = importlib.util.find_spec("pydantic_ai") is not None

# %% [markdown]
# ## 1. Setup — an in-memory governed VFS and an agent identity
#
# `demo_setup` is boilerplate you rarely step through: it builds a VFS on SQLite +
# local-FS blobs (zero infrastructure), then provisions an `agent` principal with
# `read/write/delete/execute` on `/`. Every call later in the notebook acts *as*
# that principal, so the governance you see is real, not simulated.


# %%
async def demo_setup(tmp_dir: str) -> tuple[VFS, str, str]:
    """Build the VFS and return ``(vfs, namespace_id, agent_principal_id)``."""
    config = VFSConfig(
        metadata_store_uri=f"sqlite:///{tmp_dir}/demo.db",
        blob_store_uri=f"file:///{tmp_dir}/blobs/",
        otel_enabled=False,
        audit_log_enabled=False,
        blob_cache_enabled=False,
    )
    vfs = VFS(config)
    await vfs.initialize()

    namespace = await vfs.create_namespace("codemode", "system")
    admin = await vfs.create_principal("admin")
    await vfs.bootstrap_admin(admin.id, namespace.id)
    agent = await vfs.create_principal("agent")
    await vfs.grant(admin.id, agent.id, namespace.id, "/", {"read", "write", "delete", "execute"})
    return vfs, namespace.id, agent.id


# %%
tmp_dir = tempfile.mkdtemp(prefix="codemode-demo-")
vfs, ns_id, agent_id = await demo_setup(tmp_dir)
print(f"namespace={ns_id}\nagent={agent_id}")

# %% [markdown]
# ## 2. Code mode — the agent runs Python against the governed VFS
#
# `vfs.execute` mounts the governed store as the sandbox's filesystem and runs the
# code there. It never raises for an execution-time failure: it returns an
# `ExecutionResult` whose `success`/`output` carry the value, or whose `error_type`
# names a structured failure with no host path or traceback. Here the agent does
# arithmetic — nothing touches the filesystem yet, but the result shape is the
# contract every later call obeys.

# %%
if HAS_MONTY:
    result = await vfs.execute(
        code="6 * 7",
        namespace_id=ns_id,
        principal_id=agent_id,
        provider_name="monty",
        resource_limits=ResourceLimits(timeout_seconds=10.0),
    )
    print(result)  # the whole ExecutionResult — success, output, error_type, error_message
else:
    print("pydantic-monty not installed — run `uv sync` to enable the sandbox cells")

# %% [markdown]
# ### The sandbox reads the *governed* VFS, not the host
#
# The sandbox's filesystem *is* the governed VFS: a plain `open(path).read()`
# inside the sandbox returns the VFS file's bytes, routed through the principal's
# read permission — the host filesystem is never mounted. We seed a file as the
# agent, then read it back with native `open` from inside the sandbox; the content
# crosses storage → permission check → sandbox, and comes back as an ordinary
# string. Writing back through the same mount is §3.

# %%
# not idempotent: re-running adds a version of /work/notes.txt
if HAS_MONTY:
    await vfs.write(
        namespace_id=ns_id,
        path="/work/notes.txt",
        content=b"alpha\nbeta\ngamma\n",
        principal_id=agent_id,
    )
    seen = await vfs.execute(
        code="open('/work/notes.txt').read()",
        namespace_id=ns_id,
        principal_id=agent_id,
        provider_name="monty",
        resource_limits=ResourceLimits(timeout_seconds=10.0),
    )
    print("sandbox open().read() saw:", repr(seen.output))

# %% [markdown]
# The dispatch lives in `vfs.execute` (`src/vfs/vfs.py`); the Monty mount and the
# injected verbs are in `src/vfs/execution/monty_provider.py` and `fs_ops.py`, and
# the boundary contract is `src/vfs/protocols/fs_port.py`.

# %% [markdown]
# ## 3. Writing back through the mount — native I/O, governed
#
# Editing in code mode is just native file I/O against the mount: the sandbox does
# `open(path, 'w').write(...)` and the bytes land as a new governed version, routed
# through the principal's *write* permission. No special edit verb, no separate
# surface — the same `open` you'd use on any filesystem, except this one is the VFS.
# We rewrite the file the sandbox seeded in §2, then read it back through the public
# `vfs.read` to prove the new bytes committed.

# %%
# not idempotent: re-running adds a version of /work/notes.txt
if HAS_MONTY:
    wrote = await vfs.execute(
        code="open('/work/notes.txt', 'w').write('alpha\\nBETA\\ngamma\\n')",
        namespace_id=ns_id,
        principal_id=agent_id,
        provider_name="monty",
        resource_limits=ResourceLimits(timeout_seconds=10.0),
    )
    print("sandbox write outcome:", wrote.success, wrote.error_type)
    after = await vfs.read(namespace_id=ns_id, path="/work/notes.txt", principal_id=agent_id)
    print(f"--- file now ---\n{after.decode()}")

# %% [markdown]
# The mount is `src/vfs/execution/monty_os.py`; the boundary contract every native
# read/write crosses is `src/vfs/protocols/fs_port.py`, and the dispatch that wires
# it into a sandbox is `vfs.execute` in `src/vfs/vfs.py`.

# %% [markdown]
# ## 4. Compose into a pydantic-ai tool
#
# A code-mode tool is just an async function the agent can call. We close the
# execute pattern above over `(vfs, ns_id, agent_id)` and hand it to an `Agent` as
# a `Tool` — using only names imported from the top-level `vfs` package. With a
# single sandbox tool the agent both reads and writes files by emitting native
# Python; no per-operation tool surface is needed. `TestModel` lets the agent
# construct with no credentials; swap in `"anthropic:claude-sonnet-4-6"` to drive a
# real loop.


# %%
async def execute_python(code: str) -> str:
    """Run Python in the governed sandbox; return its result or a structured error."""
    outcome = await vfs.execute(
        code=code,
        namespace_id=ns_id,
        principal_id=agent_id,
        provider_name="monty",
        resource_limits=ResourceLimits(timeout_seconds=10.0),
    )
    return repr(outcome.output) if outcome.success else f"error[{outcome.error_type}]: {outcome.error_message}"


# %%
if HAS_PYDANTIC_AI:
    from pydantic_ai import Agent, Tool
    from pydantic_ai.models.test import TestModel

    agent_tools = [Tool(execute_python, takes_ctx=False)]
    agent = Agent(TestModel(), tools=agent_tools)
    print("agent built; tools the model can call:", [tool.name for tool in agent_tools])
else:
    print("pydantic-ai not installed — run `uv sync` to enable the agent cell")

# %% [markdown]
# Everything above came from the public surface in `src/vfs/__init__.py`; the
# `tests/unit/test_consumer_samples.py` guard fails if any notebook here reaches
# into a `vfs.*` internal instead.

# %% [markdown]
# ## Cleanup
# Run this when you are done to close the VFS and remove the demo's temp files.

# %%
await vfs.close()
shutil.rmtree(tmp_dir, ignore_errors=True)
print("closed and cleaned up")
