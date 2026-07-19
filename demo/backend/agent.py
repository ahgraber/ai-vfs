"""The pydantic-ai agent and its tools — thin wrappers over a VFS `Session`.

The tools' entire reach is the VFS boundary carried in `AgentDeps`; there is
deliberately no host-filesystem tool.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import warnings

from pydantic_ai import Agent, RunContext
from pydantic_ai.capabilities import AgentCapability, Instrumentation, ProcessHistory
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.providers.openai import OpenAIProvider

from vfs import VFS, ResourceLimits, Session
from vfs.models import SearchType

from .history import build_compactor

_log = logging.getLogger(__name__)

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
    """Run a Python snippet in the Monty sandbox. Deprecated — prefer `run_code`, which runs the same sandbox but also exposes the other tools as callable functions; kept working for now.

    Monty is a *subset* of Python — not a full interpreter. Importable stdlib modules: `sys`, `typing`, `asyncio`, `math`, `json`, `re`, `datetime`, `os`, `pathlib` — and nothing else; no third-party packages, no `class` definitions, no `import *`. File I/O via `open`/`pathlib`/`os` reads and writes the same files the other tools see (no network). The value of the *last expression* is returned; you do not need to `print` it. Each run is time- and resource-limited (~15s, capped operations and read size).
    """
    warnings.warn("run_python is deprecated; use run_code (code mode).", DeprecationWarning, stacklevel=2)
    result = await ctx.deps.vfs.execute(
        code, ctx.deps.namespace_id, ctx.deps.principal_id, "monty", resource_limits=EXEC_LIMITS
    )
    return f"[deprecated: prefer run_code] {_fmt_exec(result)}"


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


def _collapse_listing(paths: list[str], path: str) -> list[str]:
    """Collapse full file paths to the entries directly under `path` (one `ls` level).

    Files at this level are returned as their basename; anything deeper is folded to its
    immediate subdirectory name with a trailing ``/`` (deduplicated). The VFS is a flat
    path namespace with no directory objects, so subdirectories exist only as prefixes of
    deeper files — this is what surfaces them.
    """
    base = path if path.startswith("/") else "/" + path
    prefix = base if base.endswith("/") else base + "/"
    entries: set[str] = set()
    for p in paths:
        if not p.startswith(prefix):
            continue
        head, sep, _ = p[len(prefix) :].partition("/")
        if head:
            entries.add(head + "/" if sep else head)
    return sorted(entries)


async def list_dir(ctx: RunContext[AgentDeps], path: str = "/", recursive: bool = False) -> str:
    """List a directory, like `ls`. Returns the entries directly under `path`: files as their name, subdirectories with a trailing `/`. Pass `recursive=True` to instead list every file path beneath `path` as full paths (like `ls -R`)."""
    metas = await _session(ctx.deps).list(path, recursive=True)
    if recursive:
        paths = sorted(m.path for m in metas)[:200]
        return "\n".join(paths) if paths else f"(empty under {path})"
    entries = _collapse_listing([m.path for m in metas], path)[:200]
    return "\n".join(entries) if entries else f"(empty under {path})"


async def search_content(ctx: RunContext[AgentDeps], query: str, mode: str = "regex") -> str:
    """Search file *contents*, like `grep`. Each hit is returned as `path:line: matched text`, so `read_file` can then fetch that span.

    `mode` selects how `query` matches:

    - regex: match `query` as a regular expression against each line (default; a plain string is a valid regex, so use one for a literal substring).
    - words: treat `query` as a set of words and return files whose contents contain all of them (ranked full-text search, not line-oriented).
    """
    search_type = SearchType.FULLTEXT if mode == "words" else SearchType.REGEX
    results = await _session(ctx.deps).search(query, "/", search_type)
    return _render_hits(results)


async def find_files(ctx: RunContext[AgentDeps], pattern: str, kind: str = "name") -> str:
    """Find files by *name/path*, like `find` — returns matching paths, not contents.

    `kind` selects how `pattern` matches:

    - name: match `pattern` against the filename (basename) at any depth; `*.md` finds every `.md` file regardless of directory (default).
    - glob: match `pattern` against the full path (`/` and `**` are meaningful); `**/*.md` matches at any depth, `*.md` only the direct children of root.
    """
    search_type = SearchType.GLOB if kind == "glob" else SearchType.FIND
    results = await _session(ctx.deps).search(pattern, "/", search_type)
    return _render_hits(results)


async def undo(ctx: RunContext[AgentDeps], path: str) -> str:
    """Undo the last change to a file, restoring the content of its previous version. This appends a new version rather than erasing history, so calling `undo` again returns the file to where it started (undo/redo toggle). Also brings back a file that was just deleted."""
    session = _session(ctx.deps)
    history = await session.versions(path, limit=2)
    if len(history) < 2:
        return f"nothing to undo for {path} (only one version)"
    target = history[1].version_number
    version = await session.rollback(path, target)
    return f"undid {path} -> restored v{target}'s content as v{version.version_number} ({version.size} bytes)"


async def delete_file(ctx: RunContext[AgentDeps], path: str) -> str:
    """Delete the file at an absolute path."""
    version = await _session(ctx.deps).delete(path)
    return f"deleted {path} (tombstone v{version.version_number})"


FILE_TOOLS = [read_file, write_file, list_dir, search_content, find_files, undo, delete_file]

#: File tools exposed as callable functions inside code mode's `run_code`. Access is via async
#: tool dispatch, not an `os` mount: the harness runs Monty synchronously on the event loop, so a
#: filesystem mount that bridged back to that same loop would deadlock. Awaited tool-functions are
#: the harness's async-native channel and route through the governed VFS unchanged.
CODEMODE_TOOLS = FILE_TOOLS

INSTRUCTIONS = (
    "You are a helpful assistant operating entirely inside a virtual filesystem. "
    "Be concise. Use your tools to read, search, run code against, and edit the files. "
    "For multi-step file work, prefer writing one `run_code` program over many separate tool calls. "
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

    Tool-surface flags in `enabled_sets` (see `Settings.enabled_sets`):

    - `codemode`: a `CodeMode` capability adds a single `run_code` tool wrapping `CODEMODE_TOOLS`
      as callable functions the model invokes with `await` inside one Python program.
    - `files`: the file tools as ordinary (native) tool calls. With `codemode` also on they are
      exposed only through `run_code` (code mode wins), and a warning is logged.
    - `bash`: `run_bash` (just-bash shell). `python`: the deprecated `run_python` (Monty) tool.
    """
    compactor = build_compactor(model, token_budget=int(context_window_tokens * compact_fraction))
    capabilities: list[AgentCapability] = [Instrumentation(), ProcessHistory(compactor)]
    code_mode = "codemode" in enabled_sets
    if code_mode and "files" in enabled_sets:
        _log.warning(
            "AIVFS_TOOLS enables both 'files' and 'codemode'; the file tools are exposed as "
            "run_code functions (code mode wins), not as native tools."
        )
    if code_mode:
        from pydantic_ai_harness import CodeMode

        # TODO(codemode): code mode runs its own Monty REPL and bypasses vfs.execute, so the
        # EXEC_LIMITS budget (timeout, max operations, read/result caps) that guards
        # run_python/run_bash is NOT enforced for run_code. Re-impose an equivalent per-run limit.
        capabilities.append(CodeMode(tools=[t.__name__ for t in CODEMODE_TOOLS]))
    agent = Agent(
        model,
        deps_type=AgentDeps,
        instructions=INSTRUCTIONS,
        capabilities=capabilities,
    )
    # File tools register when files are on, or when code mode needs them present to wrap.
    if "files" in enabled_sets or code_mode:
        for tool in FILE_TOOLS:
            agent.tool(tool)
    if "bash" in enabled_sets:
        agent.tool(run_bash)
    if "python" in enabled_sets:
        agent.tool(run_python)
    return agent


def registered_tool_names(enabled_sets: set[str]) -> list[str]:
    """Return the top-level tool names `build_agent` exposes for `enabled_sets`.

    Under `codemode` the file tools become `run_code` sandbox functions, so they are not
    listed as top-level tools.
    """
    names: list[str] = []
    if "codemode" in enabled_sets:
        names.append("run_code")
    elif "files" in enabled_sets:
        names += [t.__name__ for t in FILE_TOOLS]
    if "bash" in enabled_sets:
        names.append("run_bash")
    if "python" in enabled_sets:
        names.append("run_python")
    return sorted(set(names))
