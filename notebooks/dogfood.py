# %% [markdown]
# # ai-vfs — dogfooding a code-mode agent
#
# **The problem.** ai-vfs is a governed virtual filesystem with a code-mode
# sandbox: an agent can run Python (`monty`) or bash (`just-bash`) that reads and
# writes *the VFS*, never the host disk. This notebook stands that up as if it
# were a webserver with no local filesystem — a real LLM agent whose only reach
# into "files" is the VFS — and gives you a window to watch what it does.
#
# **Mental model** (each noun is defined where it first appears):
#
# - **VFS** — the store. Content-addressed blobs + versioned metadata.
# - **namespace** — a tenant/root the files live under.
# - **principal** — an identity (user or agent) that permissions attach to.
# - **session** — a principal bound to a namespace with a cwd; the handle every
#   operation goes through.
# - **execution provider** — a sandbox (`monty` / `just-bash`) whose filesystem
#   *is* the VFS.
# - **agent** — a pydantic-ai agent whose tools are thin wrappers over the above.
#
# **Data flow.** browser chat → pydantic-ai agent → a tool call → a `Session`
# over the VFS → sandbox or metadata store. The host filesystem is never on that
# path; the agent literally has no tool that can reach it.
#
# **How to run.** Open in VS Code Interactive or Jupyter; the kernel owns the
# event loop, so cells use top-level `await` directly. Edit any input and re-run
# to explore (git holds the original). Cells are safe to re-run unless marked
# `# not idempotent`; a `Restart → Run All` resets everything.
#
# **Prerequisites.** The code-mode extras (`uv sync --extra codemode`) — already
# present here — and, for the *chat* cells, a local OpenAI-compatible model
# (LM Studio, Ollama, mlx) at `OPENAI_BASE_URL`. The setup, seeding, code-mode,
# and introspection cells all run without a model.

# %%
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import difflib
import logging
import os
import pathlib
import shutil
import tempfile

import httpx
from pydantic_ai import Agent, RunContext
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.providers.openai import OpenAIProvider
import uvicorn

from vfs import VFS, ResourceLimits, Session, VFSConfig
from vfs.models import SearchType

logging.basicConfig(level=logging.WARNING)

# %% [markdown]
# ## Configuration lives in one cell
#
# Everything tunable is an environment variable with a sane default, so the same
# notebook drives LM Studio, Ollama, or mlx without edits. `AIVFS_TOOLS` selects
# which tool sets the agent gets — `code` (run_python/run_bash), `files`
# (read/write/list/search/delete), or `all` — the differential enablement you'd
# otherwise pass as a CLI flag.

# %%
MODEL_NAME = os.environ.get("AIVFS_MODEL", "Qwen3.6-35B-A3B-4bit")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "http://localhost:11434/v1")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "omlx")  # placeholder; local servers ignore it
API_STYLE = os.environ.get("AIVFS_API_STYLE", "chat")  # "chat" | "responses"
TOOL_SETS = os.environ.get("AIVFS_TOOLS", "all")  # "code" | "files" | "all" (comma-ok)
WEB_HOST = os.environ.get("AIVFS_HOST", "127.0.0.1")
WEB_PORT = int(os.environ.get("AIVFS_PORT", "7171"))

# Sandbox budget for a single code-mode run.
EXEC_LIMITS = ResourceLimits(
    timeout_seconds=15.0,
    max_operations=200,
    max_read_bytes=1_000_000,
    max_result_items=500,
)

# Which tool sets to register (normalized to a set of {"code", "files"}).
ENABLED_SETS = {s.strip() for s in TOOL_SETS.split(",")}
if "all" in ENABLED_SETS:
    ENABLED_SETS = {"code", "files"}

# Repo root holds `.specs/`. Prefer an explicit override, else search cwd upward.
_env_root = os.environ.get("AIVFS_REPO_ROOT")
if _env_root:
    REPO_ROOT = pathlib.Path(_env_root)
else:
    REPO_ROOT = next(
        (p for p in [pathlib.Path.cwd(), *pathlib.Path.cwd().parents] if (p / ".specs").is_dir()),
        None,
    )
if REPO_ROOT is None or not (REPO_ROOT / ".specs").is_dir():
    raise RuntimeError("Could not locate .specs/; set AIVFS_REPO_ROOT to the repo root.")
print(f"repo root : {REPO_ROOT}")
print(f"model     : {MODEL_NAME} via {OPENAI_BASE_URL} ({API_STYLE})")
print(f"tool sets : {sorted(ENABLED_SETS)}")

# %% [markdown]
# ## Is the local model reachable?
#
# The chat cells need an OpenAI-compatible endpoint; the rest of the notebook
# does not. This probe sets `MODEL_ONLINE` so those cells can skip gracefully
# instead of hanging — no traceback when the model is simply not running.


# %%
async def probe_model(base_url: str) -> bool:
    """Return True if `<base_url>/models` answers; print guidance and return False otherwise."""
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(base_url.rstrip("/") + "/models")
    except Exception as exc:  # noqa: BLE001 — a probe: any failure means "offline"
        print(f"model offline at {base_url} ({exc!r}); chat cells will skip.")
        return False
    else:
        return resp.status_code < 500


MODEL_ONLINE = await probe_model(OPENAI_BASE_URL)
print("MODEL_ONLINE =", MODEL_ONLINE)

# %% [markdown]
# ## Setup builds the VFS and two principals
#
# `demo_setup` is boilerplate you rarely step through, so it stays a function.
# It builds a VFS on SQLite + local-blob backends under a temp dir (torn down at
# the end — the point is that *your* files are never exposed, not that the store
# is exotic), then provisions:
#
# - **admin** — full rights on `/`, used only to grant others.
# - **agent** — `read + write + delete + execute` on `/`; this is the identity
#   the LLM acts as. It is *not* an admin — a deliberately constrained principal.


# %%
async def demo_setup(tmp_dir: str) -> tuple[VFS, str, str, str]:
    """Construct VFS, a namespace, and the admin + agent principals."""
    config = VFSConfig(
        metadata_store_uri=f"sqlite:///{tmp_dir}/demo.db",
        blob_store_uri=f"file:///{tmp_dir}/blobs/",
        otel_enabled=False,
        audit_log_enabled=False,
        blob_cache_enabled=False,
    )
    vfs = VFS(config)
    await vfs.initialize()

    ns = await vfs.create_namespace("dogfood", "system")
    admin = await vfs.create_principal("admin", principal_type="user")
    await vfs.bootstrap_admin(admin.id, ns.id)
    await vfs.grant(admin.id, admin.id, ns.id, "/", {"admin", "read", "write", "delete", "execute"})

    agent_principal = await vfs.create_principal("agent", principal_type="agent")
    await vfs.grant(admin.id, agent_principal.id, ns.id, "/", {"read", "write", "delete", "execute"})

    print(f"namespace : {ns.id}")
    print(f"admin     : {admin.id}")
    print(f"agent     : {agent_principal.id}")
    return vfs, ns.id, admin.id, agent_principal.id


tmp_dir = tempfile.mkdtemp(prefix="vfs-dogfood-")
vfs, ns_id, admin_id, agent_id = await demo_setup(tmp_dir)

# %% [markdown]
# ## Seed the VFS with this repo's own specs
#
# Dogfooding literally: we load the project's current contract specs
# (`.specs/specs/**/spec.md` plus `NORTH-STAR.md`) into the VFS so the agent has
# real material to explore and edit. Paths are mirrored under `/specs/` with the
# north star at the root.


# %%
# not idempotent: each run re-writes every file, bumping its version number.
async def seed_specs(vfs: VFS, ns_id: str, principal_id: str, repo_root: pathlib.Path) -> list[str]:
    """Write the baseline specs into the VFS; return the VFS paths created."""
    session = Session(vfs, ns_id, principal_id)
    sources: list[tuple[pathlib.Path, str]] = [(repo_root / ".specs" / "NORTH-STAR.md", "/NORTH-STAR.md")]
    for spec in sorted((repo_root / ".specs" / "specs").glob("*/spec.md")):
        area = spec.parent.name
        sources.append((spec, f"/specs/{area}/spec.md"))

    written: list[str] = []
    for src, vfs_path in sources:
        await session.write(vfs_path, src.read_bytes())
        written.append(vfs_path)
    return written


seeded = await seed_specs(vfs, ns_id, admin_id, REPO_ROOT)
print(f"seeded {len(seeded)} files:")
for p in seeded:
    print("  ", p)

# %% [markdown]
# ## Introspection helpers — your window into the VFS
#
# The agent mutates the VFS in the browser; these let *you* watch from the
# notebook. `tree`/`ls` list state, `cat` reads a file, and `view_diff` shows a
# unified diff between two versions — the version history the VFS keeps for every
# write. They are read-only and safe to re-run at any time.


# %%
async def tree(vfs: VFS, ns_id: str, principal_id: str, prefix: str = "/") -> None:
    """Print every path under `prefix` as an indented tree."""
    session = Session(vfs, ns_id, principal_id)
    metas = await session.list(prefix, recursive=True)
    paths = sorted(m.path for m in metas)
    if not paths:
        print(f"(empty under {prefix})")
        return
    for path in paths:
        depth = path.strip("/").count("/")
        print("  " * depth + "└─ " + path.rsplit("/", 1)[-1] + f"   ({path})")


async def ls(vfs: VFS, ns_id: str, principal_id: str, prefix: str = "/") -> list[str]:
    """Return the immediate child paths (one level) under `prefix`."""
    session = Session(vfs, ns_id, principal_id)
    metas = await session.list(prefix, recursive=True)
    root = prefix if prefix.endswith("/") else prefix + "/"
    children = {root + m.path[len(root) :].split("/", 1)[0] for m in metas if m.path.startswith(root)}
    return sorted(children)


async def cat(vfs: VFS, ns_id: str, principal_id: str, path: str, limit: int | None = 800) -> None:
    """Print a file's current version. `limit=None` prints the whole file; otherwise truncate to `limit` chars."""
    session = Session(vfs, ns_id, principal_id)
    body = (await session.read(path)).decode("utf-8", errors="replace")
    if limit is None or len(body) <= limit:
        print(body)
    else:
        print(body[:limit] + "…")


async def view_diff(
    vfs: VFS, ns_id: str, principal_id: str, path: str, older: int | None = None, newer: int | None = None
) -> None:
    """Print a unified diff between two versions of `path` (defaults to the two newest)."""
    session = Session(vfs, ns_id, principal_id)
    history = await session.versions(path)  # newest-first
    if len(history) < 2:
        print(f"{path}: only one version (v{history[0].version_number if history else '—'}); nothing to diff.")
        return
    newer = newer if newer is not None else history[0].version_number
    older = older if older is not None else history[1].version_number
    old_text = (await session.read(path, version_number=older)).decode("utf-8", errors="replace")
    new_text = (await session.read(path, version_number=newer)).decode("utf-8", errors="replace")
    diff = difflib.unified_diff(
        old_text.splitlines(),
        new_text.splitlines(),
        fromfile=f"{path}@v{older}",
        tofile=f"{path}@v{newer}",
        lineterm="",
    )
    printed = False
    for line in diff:
        print(line)
        printed = True
    if not printed:
        print(f"{path}: v{older} and v{newer} are identical.")


# Prove the seed landed: the specs tree the agent will work against.
await tree(vfs, ns_id, admin_id, "/specs")

# %% [markdown]
# ## Code-mode proof: the sandbox's filesystem *is* the VFS
#
# Before any LLM is involved, run code directly through the sandbox to show the
# boundary. In `monty`, a plain `open()` resolves against the VFS — so this reads
# the seeded north star even though that path does not exist on the host disk.
# `just-bash` gives the same guarantee for shell commands. This is the whole
# pitch: code runs, "files" work, the host filesystem is never touched.

# %%
py_result = await vfs.execute(
    "print(open('/NORTH-STAR.md').read()[:160])",
    ns_id,
    agent_id,
    "monty",
    resource_limits=EXEC_LIMITS,
)
print("monty success:", py_result.success)
print(py_result.output if py_result.success else f"[{py_result.error_type}] {py_result.error_message}")

# %%
bash_result = await vfs.execute(
    "cat /specs/session/spec.md | head -n 3",
    ns_id,
    agent_id,
    "just-bash",
    resource_limits=EXEC_LIMITS,
)
print("just-bash success:", bash_result.success)
print(bash_result.output if bash_result.success else f"[{bash_result.error_type}] {bash_result.error_message}")

# %% [markdown]
# ## The agent's tools are thin wrappers over a session
#
# Deps carry only what a tool needs to reach the VFS — the vfs handle, namespace,
# and the *agent* principal. Every tool builds a fresh `Session` from those and
# calls one VFS operation. There is deliberately no host-filesystem tool: the
# agent's entire reach is the surface below.


# %%
@dataclass
class AgentDeps:
    """What every tool receives via `RunContext.deps` — the VFS boundary, nothing more."""

    vfs: VFS
    namespace_id: str
    principal_id: str


def _session(deps: AgentDeps) -> Session:
    return Session(deps.vfs, deps.namespace_id, deps.principal_id)


def _fmt_exec(result) -> str:
    if result.success:
        return str(result.output) if result.output is not None else "(ok, no output)"
    return f"ERROR [{result.error_type}]: {result.error_message}"


# --- code set ---
async def run_python(ctx: RunContext[AgentDeps], code: str) -> str:
    """Run Python in the sandboxed VFS (monty). `open()`, `pathlib`, and `os` route to the VFS; no host filesystem access."""
    result = await ctx.deps.vfs.execute(
        code, ctx.deps.namespace_id, ctx.deps.principal_id, "monty", resource_limits=EXEC_LIMITS
    )
    return _fmt_exec(result)


async def run_bash(ctx: RunContext[AgentDeps], code: str) -> str:
    """Run bash over the VFS (just-bash). File ops, pipes, and grep/find/glob resolve against the VFS, not the host."""
    result = await ctx.deps.vfs.execute(
        code, ctx.deps.namespace_id, ctx.deps.principal_id, "just-bash", resource_limits=EXEC_LIMITS
    )
    return _fmt_exec(result)


# --- files set ---
async def read_file(ctx: RunContext[AgentDeps], path: str) -> str:
    """Read a file from the VFS by absolute path."""
    return (await _session(ctx.deps).read(path)).decode("utf-8", errors="replace")


async def write_file(ctx: RunContext[AgentDeps], path: str, content: str) -> str:
    """Write (create or new-version) a file in the VFS; returns the new version number."""
    version = await _session(ctx.deps).write(path, content.encode("utf-8"))
    return f"wrote {path} -> v{version.version_number} ({version.size} bytes)"


async def list_dir(ctx: RunContext[AgentDeps], path: str = "/") -> str:
    """List every file path under a prefix in the VFS."""
    metas = await _session(ctx.deps).list(path, recursive=True)
    paths = sorted(m.path for m in metas)[:200]
    return "\n".join(paths) if paths else f"(empty under {path})"


async def search_files(ctx: RunContext[AgentDeps], query: str, kind: str = "glob") -> str:
    """Search the VFS. `kind` is one of glob | regex | find | fulltext."""
    search_type = {
        "glob": SearchType.GLOB,
        "regex": SearchType.REGEX,
        "find": SearchType.FIND,
        "fulltext": SearchType.FULLTEXT,
    }[kind]
    results = await _session(ctx.deps).search(query, "/", search_type)
    lines = [f"{r.path}" + (f":{r.line_number}" if r.line_number else "") for r in results[:100]]
    return "\n".join(lines) if lines else "(no matches)"


async def delete_file(ctx: RunContext[AgentDeps], path: str) -> str:
    """Delete (tombstone) a file in the VFS."""
    version = await _session(ctx.deps).delete(path)
    return f"deleted {path} (tombstone v{version.version_number})"


CODE_TOOLS = [run_python, run_bash]
FILE_TOOLS = [read_file, write_file, list_dir, search_files, delete_file]

# %% [markdown]
# ## Build the agent with the enabled tool sets
#
# `build_agent` picks the model class from `API_STYLE`, points it at the local
# endpoint, and registers only the tool sets `AIVFS_TOOLS` selected. The system
# prompt tells the model it operates entirely inside the VFS.

# %%
INSTRUCTIONS = (
    "You are a coding agent operating inside a virtual filesystem (VFS). "
    "You have no access to any host filesystem — your tools are your only way to touch files. "
    "This project's specs are mounted at /specs/<area>/spec.md and /NORTH-STAR.md. "
    "Prefer code-mode (run_python / run_bash) for multi-step work; use the file tools for simple reads and edits. "
    "Always use absolute VFS paths."
)


def build_model():
    provider = OpenAIProvider(base_url=OPENAI_BASE_URL, api_key=OPENAI_API_KEY)
    if API_STYLE == "responses":
        return OpenAIResponsesModel(MODEL_NAME, provider=provider)
    return OpenAIChatModel(MODEL_NAME, provider=provider)


def build_agent(enabled_sets: set[str]) -> Agent[AgentDeps, str]:
    agent = Agent(build_model(), deps_type=AgentDeps, instructions=INSTRUCTIONS)
    if "code" in enabled_sets:
        for tool in CODE_TOOLS:
            agent.tool(tool)
    if "files" in enabled_sets:
        for tool in FILE_TOOLS:
            agent.tool(tool)
    return agent


agent = build_agent(ENABLED_SETS)
deps = AgentDeps(vfs=vfs, namespace_id=ns_id, principal_id=agent_id)
registered = sorted(
    t.__name__
    for t in (CODE_TOOLS if "code" in ENABLED_SETS else []) + (FILE_TOOLS if "files" in ENABLED_SETS else [])
)
print(f"agent tools ({len(registered)}): {registered}")

# %% [markdown]
# ## Launch the web chat UI
#
# `agent.to_web()` returns a Starlette app; we serve it with uvicorn as a task on
# the kernel's own event loop (never a competing loop). It runs in the
# background, so the introspection cells below stay responsive while you chat in
# the browser at the printed URL. Signal handlers are disabled because we are not
# the main thread's owner here.

# %%
web_app = agent.to_web(deps=deps)
_web_config = uvicorn.Config(web_app, host=WEB_HOST, port=WEB_PORT, log_level="warning")
web_server = uvicorn.Server(_web_config)
web_server.install_signal_handlers = lambda: None  # notebook: don't hijack SIGINT
web_task = asyncio.create_task(web_server.serve())
await asyncio.sleep(1.0)
print("web UI started:", web_server.started, f"→ http://{WEB_HOST}:{WEB_PORT}")
if not MODEL_ONLINE:
    print("(the model is offline; the UI loads but replies will fail until a local model is running)")

# %% [markdown]
# ## Or drive it inline, no browser needed
#
# The same agent runs from a cell. This is the fastest way to smoke-test tool
# wiring. It skips itself when the local model is offline so `Restart → Run All`
# always passes.

# %%
if MODEL_ONLINE:
    result = await agent.run(
        "Use your tools to list what's under /specs, then summarize the session spec in two sentences.",
        deps=deps,
    )
    print(result.output)
else:
    print(f"Local model offline at {OPENAI_BASE_URL}; start LM Studio/Ollama/mlx and re-run. Skipping inline run.")

# %% [markdown]
# ## Interact with what changed

# %%
await tree(vfs, ns_id, admin_id, "/specs")

# %%
spec = await cat(
    vfs,
    ns_id,
    admin_id,
    "/specs/hashline-anchors/spec.md",
    limit=None,
)
print(spec)

# %%
await view_diff(vfs, ns_id, admin_id, "/specs/hashline-anchors/spec.md")

# %% [markdown]
# ## Teardown
#
# Stop the web server, close the VFS, and delete the temp store. Run this when
# you're done — it is the counterpart to setup and leaves nothing on disk.

# %%
web_server.should_exit = True
await web_task
await vfs.close()
shutil.rmtree(tmp_dir, ignore_errors=True)
print(f"stopped web server, closed VFS, removed {tmp_dir}")

# %%
