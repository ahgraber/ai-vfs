"""The pydantic-ai agent and its tools — thin wrappers over a VFS `Session`.

The tools' entire reach is the VFS boundary carried in `AgentDeps`; there is
deliberately no host-filesystem tool.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic_ai import Agent, RunContext
from pydantic_ai.capabilities import Instrumentation, ProcessHistory
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.providers.openai import OpenAIProvider

from vfs import VFS, ResourceLimits, Session
from vfs.models import SearchType

from .history import build_compactor

# Sandbox budget for a single code-mode run.
EXEC_LIMITS = ResourceLimits(
    timeout_seconds=15.0,
    max_operations=200,
    max_read_bytes=1_000_000,
    max_result_items=500,
)

#: Lines returned by an unbounded `read_file`; longer files are truncated to this window.
READ_DEFAULT_LINES = 200


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


def _render_lines(text: str, start: int | None, end: int | None) -> str:
    """Render a file for reading: a numbered span, or a capped default window.

    With `start`/`end` (1-based, inclusive) the selected lines are prefixed with their line
    numbers (``cat -n`` style), clamped to the file's extent, so the model can request
    follow-up spans by number. With neither bound the read defaults to the first
    ``READ_DEFAULT_LINES`` lines: a file within that many lines is returned unchanged; a
    longer file is truncated to the window, numbered, and followed by a footer giving the
    total line count and how to page on.
    """
    lines = text.splitlines()
    if start is None and end is None:
        if len(lines) <= READ_DEFAULT_LINES:
            return text
        window = "\n".join(f"{n}\t{lines[n - 1]}" for n in range(1, READ_DEFAULT_LINES + 1))
        return f"{window}\n\n-- showing lines 1-{READ_DEFAULT_LINES} of {len(lines)}; pass start/end to read more --"
    lo = max(1, start if start is not None else 1)
    hi = min(len(lines), end if end is not None else len(lines))
    if lo > hi:
        return f"(no lines in requested span; file has {len(lines)} lines)"
    return "\n".join(f"{n}\t{lines[n - 1]}" for n in range(lo, hi + 1))


def _render_hits(results) -> str:
    """Format search hits as ``path[:line][: matched text]``, capped at 100 lines."""
    lines: list[str] = []
    for r in results[:100]:
        loc = r.path + (f":{r.line_number}" if r.line_number else "")
        lines.append(f"{loc}: {r.match_context}" if r.match_context else loc)
    return "\n".join(lines) if lines else "(no matches)"


# --- code set ---
async def run_python(ctx: RunContext[AgentDeps], code: str) -> str:
    """Run a Python snippet. File operations (`open`, `pathlib`, `os`) read and write the same files the other tools see; there is no network access. Each run is time- and resource-limited (~15s, capped file operations and read size), so keep snippets small and avoid unbounded loops or very large reads."""
    result = await ctx.deps.vfs.execute(
        code, ctx.deps.namespace_id, ctx.deps.principal_id, "monty", resource_limits=EXEC_LIMITS
    )
    return _fmt_exec(result)


async def run_bash(ctx: RunContext[AgentDeps], code: str) -> str:
    """Run a bash snippet. File commands, pipes, and tools like `grep`, `find`, and `glob` operate on the same files the other tools see; there is no network access. Each run is time- and resource-limited (~15s, capped file operations and read size), so avoid long-running or unbounded commands."""
    result = await ctx.deps.vfs.execute(
        code, ctx.deps.namespace_id, ctx.deps.principal_id, "just-bash", resource_limits=EXEC_LIMITS
    )
    return _fmt_exec(result)


# --- files set ---
async def read_file(ctx: RunContext[AgentDeps], path: str, start: int | None = None, end: int | None = None) -> str:
    """Read a file by absolute path. Windowed reads are standard practice: omit `start`/`end` to read the first 200 lines (a longer file is truncated to that window with a footer giving the total line count and how to continue); pass `start`/`end` (1-based, inclusive) to read a specific line span. Windowed and spanned output carries line-number prefixes, so request the next window by number. Prefer paging through windows to reading whole files, to preserve context."""
    text = (await _session(ctx.deps).read(path)).decode("utf-8", errors="replace")
    return _render_lines(text, start, end)


async def write_file(ctx: RunContext[AgentDeps], path: str, content: str) -> str:
    """Create or update a file at an absolute path. Each write saves a new version rather than overwriting; returns the new version number."""
    version = await _session(ctx.deps).write(path, content.encode("utf-8"))
    return f"wrote {path} -> v{version.version_number} ({version.size} bytes)"


async def list_dir(ctx: RunContext[AgentDeps], path: str = "/") -> str:
    """List every file path under a directory prefix, recursively."""
    metas = await _session(ctx.deps).list(path, recursive=True)
    paths = sorted(m.path for m in metas)[:200]
    return "\n".join(paths) if paths else f"(empty under {path})"


async def search_files(ctx: RunContext[AgentDeps], query: str, kind: str = "glob") -> str:
    """Search files. `kind` selects what `query` matches.

    - glob: match a path pattern (`/` and `**` are meaningful); use `**/*.md` to match at any depth, `*.md` for direct children only.
    - find: match a filename pattern against the basename at any depth; `*.md` finds every `.md` file regardless of directory.
    - regex: match a regular expression against file contents; each hit carries the matching line number and line text, so `read_file` can then fetch just that span.
    - fulltext: match words in file contents; hits carry the matching line number and line text.
    """
    search_type = {
        "glob": SearchType.GLOB,
        "regex": SearchType.REGEX,
        "find": SearchType.FIND,
        "fulltext": SearchType.FULLTEXT,
    }[kind]
    results = await _session(ctx.deps).search(query, "/", search_type)
    return _render_hits(results)


async def delete_file(ctx: RunContext[AgentDeps], path: str) -> str:
    """Delete the file at an absolute path."""
    version = await _session(ctx.deps).delete(path)
    return f"deleted {path} (tombstone v{version.version_number})"


CODE_TOOLS = [run_python, run_bash]
FILE_TOOLS = [read_file, write_file, list_dir, search_files, delete_file]

INSTRUCTIONS = (
    "You are a helpful assistant operating entirely inside a virtual filesystem. "
    "Be concise. Use your tools to read, search, run code against, and edit the files. "
    "At the end of a turn, briefly narrate which tools you used; if a tool errored, "
    "say which tool and what the error was."
)


def build_model(model_name: str, base_url: str, api_key: str, api_style: str) -> Model:
    """Pick the model class from `api_style` and point it at the local endpoint."""
    provider = OpenAIProvider(base_url=base_url, api_key=api_key)
    if api_style == "responses":
        return OpenAIResponsesModel(model_name, provider=provider)
    return OpenAIChatModel(model_name, provider=provider)


def build_agent(
    model: Model,
    enabled_sets: set[str],
    *,
    context_window_tokens: int = 32_768,
    compact_fraction: float = 0.6,
) -> Agent[AgentDeps, str]:
    """Register only the tool sets `enabled_sets` selected on a fresh agent.

    The `Instrumentation` capability emits an OpenTelemetry span per agent run,
    model call, and tool call. It uses the global tracer provider, so if tracing
    is wired (see `tracing.py`) the spans reach MLflow; otherwise it is a no-op.

    The `ProcessHistory` capability bounds the model's context (see `history.py`):
    once a pending request is estimated to exceed `context_window_tokens *
    compact_fraction`, the oldest messages are summarized with `model` and recent
    messages kept verbatim. It fires before every model request, so it covers both
    long conversations and long single turns.
    """
    compactor = build_compactor(model, token_budget=int(context_window_tokens * compact_fraction))
    agent = Agent(
        model,
        deps_type=AgentDeps,
        instructions=INSTRUCTIONS,
        capabilities=[Instrumentation(), ProcessHistory(compactor)],
    )
    if "code" in enabled_sets:
        for tool in CODE_TOOLS:
            agent.tool(tool)
    if "files" in enabled_sets:
        for tool in FILE_TOOLS:
            agent.tool(tool)
    return agent


def registered_tool_names(enabled_sets: set[str]) -> list[str]:
    """Return the tool names that `build_agent` would register for `enabled_sets`."""
    tools = (CODE_TOOLS if "code" in enabled_sets else []) + (FILE_TOOLS if "files" in enabled_sets else [])
    return sorted(t.__name__ for t in tools)
