# %% [markdown]
# # ai-vfs — Code-mode tools for a pydantic-ai agent
#
# An agent that edits files needs two things you normally have to build yourself:
# a place to *run code* against those files, and a way to *change a line* without
# re-emitting the whole document. ai-vfs gives both over a **governed** store —
# every read, write, and sandbox call is checked against the calling principal's
# permissions and audited — and a consumer reaches all of it through the public
# `vfs` package alone, never a `vfs.*` submodule. This notebook wires those two
# capabilities onto a [pydantic-ai](https://ai.pydantic.dev) agent as tools.
#
# A few terms used throughout:
# - **code mode** — instead of one tool per operation, give the agent a sandbox
#   and let it *write code* that calls the filesystem; `vfs.execute` runs it.
# - **sandbox mount** — the sandbox sees the governed VFS as its filesystem, not
#   the host's; it can only touch what the principal may.
# - **anchor** — a line locator `index:checksum` derived from file content, the
#   unit an anchored edit targets.
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

from vfs import VFS, AnchorConflictError, AnchoredEditor, Hunk, ResourceLimits, Session, VFSConfig

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
# string (anchored, addressable reads are §3's job).

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
# ## 3. Anchored editing — change a line without re-sending the file
#
# `AnchoredEditor` is the standalone, **stateless** edit surface: `read_anchored`
# returns the file's version plus a content-derived `index:checksum` anchor for
# every line, and `edit_anchored` replaces an anchored span — applying only when
# the file is still at the version you read (a stale edit is rejected, not merged).
# Nothing is stored between calls; the anchors are recomputed from content each
# time, which is why an agent can read, edit, and re-read across independent turns.

# %%
# not idempotent: re-running adds a version of /src/greeting.py
await vfs.write(
    namespace_id=ns_id,
    path="/src/greeting.py",
    content=b"def greet(name):\n    return 'hello ' + name\n",
    principal_id=agent_id,
)
editor = AnchoredEditor(Session(vfs, ns_id, agent_id))
read = await editor.read_anchored(path="/src/greeting.py")
print(f"version {read.version}")
for index, anchor in read.anchors.items():
    print(f"  {anchor}  ->  {read.lines[index]!r}")

# %% [markdown]
# ### One edit writes a new version under the version it read
#
# We replace the body line (its anchor from the read above) with an f-string. The
# edit carries `expected_version`, so it commits only against the exact content
# the anchor came from; the read-back proves the new bytes landed.

# %%
# not idempotent: succeeds once (v1 -> v2), then conflicts because the file moved on
body_index = 1
result = await editor.edit_anchored(
    path="/src/greeting.py",
    hunks=[
        Hunk(
            start_anchor=read.anchors[body_index],
            end_anchor=read.anchors[body_index],
            replacement=["    return f'hello {name}'"],
        )
    ],
    expected_version=read.version,
)
after = await vfs.read(namespace_id=ns_id, path="/src/greeting.py", principal_id=agent_id)
print(f"new version {result.new_version}\n--- file now ---\n{after.decode()}")

# %% [markdown]
# ### A stale anchor is rejected, not applied to the wrong line
#
# `read.version` is now behind the file. Re-using it asks to edit content that no
# longer exists at that version, so the editor raises `AnchorConflictError` and
# writes nothing — the guarantee that makes anchored editing safe for concurrent
# or multi-turn agents. Re-running this cell always rejects, changing nothing.

# %%
try:
    await editor.edit_anchored(
        path="/src/greeting.py",
        hunks=[
            Hunk(
                start_anchor=read.anchors[body_index],
                end_anchor=read.anchors[body_index],
                replacement=["    return name"],
            )
        ],
        expected_version=read.version,  # stale on purpose
    )
except AnchorConflictError as exc:
    print(f"AnchorConflictError (expected): {exc}")

# %% [markdown]
# The capability is `src/vfs/anchored_editing/editor.py` (`read_anchored`,
# `edit_anchored`); the anchor format and strict-conflict contract are specified in
# `.specs/changes/2026-06-28-sandbox-fs-mount/specs/anchored-editing/spec.md`.

# %% [markdown]
# ## 4. Compose the two into pydantic-ai tools
#
# A code-mode tool is just an async function the agent can call. We close the two
# patterns above over `(vfs, ns_id, agent_id)` and hand them to an `Agent` as
# `Tool`s — using only names imported from the top-level `vfs` package. `TestModel`
# lets the agent construct with no credentials; swap in `"anthropic:claude-sonnet-4-6"`
# to drive a real loop.


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


async def edit_line(path: str, anchor: str, replacement: str) -> str:
    """Replace the line at ``anchor`` with ``replacement`` (a fresh read+edit each call)."""
    line_editor = AnchoredEditor(Session(vfs, ns_id, agent_id))
    current = await line_editor.read_anchored(path=path)
    outcome = await line_editor.edit_anchored(
        path=path,
        hunks=[Hunk(start_anchor=anchor, end_anchor=anchor, replacement=[replacement])],
        expected_version=current.version,
    )
    return f"ok: /{path} now at version {outcome.new_version}"


# %%
if HAS_PYDANTIC_AI:
    from pydantic_ai import Agent, Tool
    from pydantic_ai.models.test import TestModel

    agent_tools = [Tool(execute_python, takes_ctx=False), Tool(edit_line, takes_ctx=False)]
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
