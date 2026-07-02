"""JustBashExecutionProvider: just-bash backed sandboxed bash over the governed VFS.

Requires the ``just-bash`` optional extra. Install with: ``pip install 'ai-vfs[just-bash]'``.

Architecture
------------
- An :class:`_JustBashFs` adapter implements just-bash's async ``IFileSystem`` by
  passing through to the session-backed FS-port — so every bash file operation
  (``cat``, redirection, pipes) enforces the principal's permissions and is
  audited, and the host filesystem is never exposed. Operations with no VFS
  equivalent (``chmod``/``symlink``/``readlink``/``utimes``) raise unsupported.
- ``grep``/``find``/``glob`` are overridden via ``commands=`` so they route to
  the VFS search index (``session.search``, reached through the shared
  ``FsOperations`` callables) instead of brute-force file enumeration — parity
  with the Monty search verbs, including the operation budget and the
  cold-index/``ReindexRequiredError`` propagation.

Spike-resolved behavior (see ``design.md``): ``commands=`` *replaces* the builtin
registry rather than merging, so the full builtin set is rebuilt via
``create_command_registry()`` and the search verbs are overridden on top.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from just_bash import Bash, ExecResult, FsStat
from just_bash.commands import create_command_registry

from vfs.errors import NotFoundError, UnsupportedOperationError

if TYPE_CHECKING:
    from just_bash import CommandContext

    from vfs.execution.fs_ops import FsOperations
    from vfs.protocols.execution import ExecutionResult, ResourceLimits
    from vfs.protocols.fs_port import FsPort


class _JustBashFs:
    """just-bash ``IFileSystem`` implemented over the session-backed FS-port."""

    def __init__(self, fs_port: FsPort) -> None:
        self._fs = fs_port

    async def read_file(self, path: str, encoding: str = "utf-8") -> str:
        return (await self._fs.read(path)).decode(encoding)

    async def read_file_bytes(self, path: str) -> bytes:
        return await self._fs.read(path)

    async def write_file(self, path: str, content: str | bytes, encoding: str = "utf-8") -> None:
        data = content.encode(encoding) if isinstance(content, str) else content
        await self._fs.write(path, data)

    async def append_file(self, path: str, content: str | bytes) -> None:
        data = content.encode("utf-8") if isinstance(content, str) else content
        try:
            existing = await self._fs.read(path)
        except NotFoundError:
            existing = b""
        await self._fs.write(path, existing + data)

    async def exists(self, path: str) -> bool:
        return await self._fs.exists(path)

    async def is_file(self, path: str) -> bool:
        try:
            return not (await self._fs.stat(path)).is_dir
        except NotFoundError:
            return False

    async def is_directory(self, path: str) -> bool:
        try:
            return (await self._fs.stat(path)).is_dir
        except NotFoundError:
            return False

    async def readdir(self, path: str) -> list[str]:
        children = await self._fs.list(path)
        return [child.rstrip("/").rsplit("/", 1)[-1] for child in children]

    async def mkdir(self, path: str, recursive: bool = False) -> None:  # noqa: ARG002
        await self._fs.mkdir(path)

    async def rm(self, path: str, recursive: bool = False, force: bool = False) -> None:  # noqa: ARG002
        await self._fs.delete(path)

    async def stat(self, path: str) -> FsStat:
        st = await self._fs.stat(path)
        return FsStat(is_file=not st.is_dir, is_directory=st.is_dir, size=st.size, mode=0o644)

    def resolve_path(self, base: str, path: str) -> str:
        from vfs.session import resolve_path

        return resolve_path(base, path)

    async def realpath(self, path: str) -> str:
        from vfs.session import resolve_path

        return resolve_path("/", path)

    # --- operations above the floor (no VFS equivalent) -----------------------

    async def chmod(self, path: str, mode: int) -> None:
        raise UnsupportedOperationError("chmod has no VFS equivalent.")

    async def symlink(self, target: str, link_path: str) -> None:
        raise UnsupportedOperationError("symlink has no VFS equivalent.")

    async def readlink(self, path: str) -> str:
        raise UnsupportedOperationError("readlink has no VFS equivalent.")

    async def utimes(self, path: str, atime: float, mtime: float) -> None:
        raise UnsupportedOperationError("utimes has no VFS equivalent.")


class _GrepCommand:
    """Overrides bash ``grep`` to route to the VFS search index via ``FsOperations``."""

    def __init__(self, fs_ops: FsOperations, cwd: str) -> None:
        self._fs_ops = fs_ops
        self._cwd = cwd

    async def execute(self, args: list[str], ctx: CommandContext) -> ExecResult:  # noqa: ARG002
        positional = [a for a in args if not a.startswith("-")]
        if not positional:
            return ExecResult(stderr="grep: missing pattern\n", exit_code=2)
        pattern = positional[0]
        scope = positional[1] if len(positional) > 1 else getattr(ctx, "cwd", self._cwd)
        result = await self._fs_ops.grep(pattern, scope)
        lines = [
            f"{r['path']}:{r['line_number']}:{r['match_context']}"
            if r.get("line_number") is not None
            else f"{r['path']}:{r['match_context']}"
            for r in result["results"]
        ]
        stdout = "\n".join(lines) + ("\n" if lines else "")
        return ExecResult(stdout=stdout, exit_code=0 if lines else 1)


class _FindCommand:
    """Overrides bash ``find`` to route to the VFS search index via ``FsOperations``."""

    def __init__(self, fs_ops: FsOperations, cwd: str) -> None:
        self._fs_ops = fs_ops
        self._cwd = cwd

    async def execute(self, args: list[str], ctx: CommandContext) -> ExecResult:  # noqa: ARG002
        scope = args[0] if args and not args[0].startswith("-") else getattr(ctx, "cwd", self._cwd)
        name = None
        if "-name" in args:
            idx = args.index("-name")
            if idx + 1 < len(args):
                name = args[idx + 1]
        result = await self._fs_ops.find(scope, **({"name": name} if name else {}))
        paths = [r["path"] for r in result["results"]]
        stdout = "\n".join(paths) + ("\n" if paths else "")
        return ExecResult(stdout=stdout, exit_code=0)


class _GlobCommand:
    """Adds a ``glob`` command routing to the VFS search index (parity with Monty)."""

    def __init__(self, fs_ops: FsOperations) -> None:
        self._fs_ops = fs_ops

    async def execute(self, args: list[str], ctx: CommandContext) -> ExecResult:  # noqa: ARG002
        if not args:
            return ExecResult(stderr="glob: missing pattern\n", exit_code=2)
        result = await self._fs_ops.glob(args[0])
        paths = [r["path"] for r in result["results"]]
        stdout = "\n".join(paths) + ("\n" if paths else "")
        return ExecResult(stdout=stdout, exit_code=0 if paths else 1)


class JustBashExecutionProvider:
    """Execution provider backed by just-bash, over the governed VFS.

    Stateless per-execution: each ``execute`` builds a fresh ``Bash`` bound to the
    FS-port adapter and the search-routed command overrides. ``reset()`` is a
    no-op; ``capabilities()`` declares a bash tier.
    """

    async def execute(
        self,
        code: str,
        fs_ops: FsOperations,
        fs_port: Any,
        resource_limits: ResourceLimits,  # noqa: ARG002 — VFS limits are enforced by the FS-port
    ) -> ExecutionResult:
        """Run bash ``code`` over the VFS; route search builtins to the index.

        VFS ``ResourceLimits`` (operation budget, ``max_read_bytes``,
        ``max_write_bytes``) are enforced by the ``fs_port``/``fs_ops`` pair that
        ``vfs.execute`` constructs from ``resource_limits`` — every bash file
        operation routes through them — so this provider does not re-enforce them.
        just-bash's own ``ExecutionLimits`` (call depth, command/loop counts) apply
        with their library defaults as an independent guard against runaway scripts.
        """
        from vfs.protocols.execution import ExecutionResult

        cwd = getattr(fs_port, "cwd", "/")
        adapter = _JustBashFs(fs_port)

        # commands= replaces the builtin registry, so rebuild it and override on top.
        registry = create_command_registry()
        registry["grep"] = _GrepCommand(fs_ops, cwd)
        registry["find"] = _FindCommand(fs_ops, cwd)
        registry["glob"] = _GlobCommand(fs_ops)

        shell = Bash(fs=adapter, cwd=cwd, commands=registry)
        # A VFS error raised inside the FS-port adapter (e.g. PermissionDeniedError)
        # propagates out of ``shell.exec`` so ``vfs.execute``'s translation table owns it.
        result = await shell.exec(code)
        if result.exit_code == 0:
            return ExecutionResult(success=True, output=result.stdout)
        # Non-zero exit: the script ran but signalled failure. Surface the exit code
        # and stderr so the caller can diagnose it instead of seeing a false success.
        # (bash stderr references VFS paths only — the host filesystem is never exposed.)
        message = result.stderr.strip() or f"Command exited with status {result.exit_code}"
        return ExecutionResult(
            success=False,
            output=result.stdout,
            error_type="nonzero_exit",
            error_message=message,
        )

    def capabilities(self) -> ExecutionCapabilities:  # noqa: F821 — forward ref resolved below
        """Return just-bash provider capabilities (async, bash, tier ``just-bash``)."""
        from vfs.protocols.execution import ExecutionCapabilities

        return ExecutionCapabilities(
            supports_async=True, language="bash", tier="just-bash", enforces_memory_limit=False
        )

    def reset(self) -> None:
        """No-op: JustBashExecutionProvider is stateless per-execution."""
