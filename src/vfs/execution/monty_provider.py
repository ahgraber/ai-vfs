"""MontyExecutionProvider: pydantic-monty backed sandboxed execution.

Requires the ``monty`` optional extra (``pydantic-monty>=0.0.18,<0.1``).
Install with: ``pip install 'ai-vfs[monty]'``

Architecture
------------
``MontyExecutionProvider.execute`` constructs a ``Monty`` runner from ``code``,
passes the async ``FsOperations`` callables as ``external_functions``, and awaits
``monty.run_async(...)`` on the host event loop.  pydantic-monty awaits
coroutine-returning external functions on the host event loop natively, so no
thread bridging is needed (confirmed by Decision (e) and empirical testing).

VFS error unwrapping
--------------------
Monty downcasts custom exception types to their nearest known built-in.
``VFSError`` subclasses that do not extend a known Python built-in are downcast
to ``Exception``, losing their type identity.  To preserve them, each
``FsOperations`` callable is wrapped by a thin sentinel that stores the first
VFS error it raises in a closure variable.  After ``run_async`` raises
``MontyRuntimeError``, the stored VFS error (if any) is re-raised directly,
allowing ``vfs.execute``'s translation table to own it as designed.

ResourceLimits field mapping (verified against pydantic-monty 0.0.18)
-----------------------------------------------------------------------
+-----------------------+-------------------+
| VFS ResourceLimits    | Monty field name  |
+=======================+===================+
| timeout_seconds       | max_duration_secs |
| max_memory_bytes      | max_memory        |
+-----------------------+-------------------+

Unmapped (unenforced at the provider level):
- max_operations  → enforced by OperationCounter in fs_operations_for
- max_read_bytes  → enforced by FsOperations cat/head/tail wrappers
- max_result_items→ enforced by FsOperations grep/find/ls wrappers
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
from typing import TYPE_CHECKING, Any

from pydantic_monty import Monty, MontyError, MontyRuntimeError, ResourceLimits as MontyResourceLimits

from vfs.errors import VFSError

if TYPE_CHECKING:
    from vfs.execution.fs_ops import FsOperations
    from vfs.protocols.execution import ExecutionCapabilities, ExecutionResult, ResourceLimits

_log = logging.getLogger(__name__)

# Public shell-wrapper names exposed to the sandbox (the ten shell ops).
# These match the FsOperations field names and the shell-ops table in the design.
_SHELL_FUNCTION_NAMES = (
    "cd",
    "pwd",
    "cat",
    "head",
    "tail",
    "ls",
    "grep",
    "find",
    "glob",
    "write",
    "edit",
)


class MontyExecutionProvider:
    """Execution provider backed by pydantic-monty.

    Stateless per-execution: each ``execute`` call receives a fresh
    ``FsOperations`` and constructs a new ``Monty`` runner.  ``reset()`` is a
    no-op; ``capabilities()`` declares async Python support at tier ``"monty"``.
    """

    async def execute(
        self,
        code: str,
        fs_ops: FsOperations,
        fs_port: Any,
        resource_limits: ResourceLimits,
    ) -> ExecutionResult:
        """Execute ``code`` in the Monty sandbox with both surfaces wired.

        The async ``fs_ops`` callables are passed as ``external_functions`` (the
        injected verbs, kept additively), and ``fs_port`` is mounted as the
        sandbox's native filesystem via :class:`~vfs.execution.monty_os.MontyVfsOS`.

        VFS errors raised inside either surface are preserved through Monty's
        exception downcast via a shared sentinel and re-raised after ``run_async``
        returns, so ``vfs.execute``'s translation table handles them.

        Monty-internal errors (syntax, runtime, timeout, memory) are converted
        to ``ExecutionResult(success=False, error_type="provider_error", ...)``.
        """
        from vfs.execution.monty_os import MontyVfsOS
        from vfs.protocols.execution import ExecutionResult

        # Shared sentinel: first VFS error raised by any fs_ops callable OR by the
        # native filesystem mount during this execution.
        _vfs_error: list[VFSError] = []  # list used as mutable cell for nonlocal capture

        def _wrap(fn: Any) -> Any:
            """Wrap an FsOperations callable to capture the first VFS error."""

            async def _wrapped(*args: Any, **kwargs: Any) -> Any:
                try:
                    return await fn(*args, **kwargs)
                except VFSError as exc:
                    if not _vfs_error:
                        _vfs_error.append(exc)
                    raise  # let Monty see the failure; we re-raise the original after

            return _wrapped

        external_functions = {name: _wrap(getattr(fs_ops, name)) for name in _SHELL_FUNCTION_NAMES}
        mount = MontyVfsOS(fs_port, asyncio.get_running_loop(), _vfs_error)

        limits: MontyResourceLimits = {}
        if resource_limits.timeout_seconds is not None:
            limits["max_duration_secs"] = float(resource_limits.timeout_seconds)
        if resource_limits.max_memory_bytes is not None:
            limits["max_memory"] = int(resource_limits.max_memory_bytes)

        runner = Monty(code)
        try:
            output = await runner.run_async(
                external_functions=external_functions,
                os=mount,
                limits=limits if limits else None,
            )
        except MontyRuntimeError as exc:
            # Retrieve Monty's terminating inner exception first — needed for
            # the correspondence check below regardless of whether _vfs_error is set.
            inner = exc.exception()
            if _vfs_error:
                captured = _vfs_error[0]
                # Re-raise the original VFS error only when Monty's terminating
                # exception corresponds to it.  If the sandbox *caught* the VFS
                # error and later failed for an unrelated reason (e.g. NameError),
                # inner will be that unrelated exception; we must NOT re-raise the
                # captured VFS error in that case — the correct result is
                # error_type="internal_error" (or whatever the unrelated exception maps to).
                #
                # Correspondence criteria (any one sufficient):
                # 1. Identity — same object (unlikely given Monty's downcast, but handled).
                # 2. Type name — pydantic-monty downcasts custom exceptions to their
                #    nearest built-in; the class name is stable even after downcast.
                # 3. Message — string representation matches.
                corresponds = (
                    inner is captured or type(inner).__name__ == type(captured).__name__ or str(inner) == str(captured)
                )
                if corresponds:
                    raise captured from exc
                # Sentinel VFS error was caught by the sandbox code; Monty failed
                # for an unrelated reason.  Fall through to provider_error handling.
            # Monty-internal runtime error (sandbox timeout, memory, etc.)
            safe_msg = _safe_error_message(exc, inner)
            _log.debug("Monty runtime error: %s", safe_msg)
            return ExecutionResult(success=False, error_type="provider_error", error_message=safe_msg)
        except MontyError as exc:
            # Syntax / type errors in the code string itself
            safe_msg = _safe_error_message(exc, None)
            _log.debug("Monty error: %s", safe_msg)
            return ExecutionResult(success=False, error_type="provider_error", error_message=safe_msg)

        return ExecutionResult(success=True, output=output)

    def capabilities(self) -> ExecutionCapabilities:
        """Return Monty provider capabilities."""
        from vfs.protocols.execution import ExecutionCapabilities

        return ExecutionCapabilities(supports_async=True, language="python", tier="monty")

    def reset(self) -> None:
        """No-op: MontyExecutionProvider is stateless per-execution."""


# Matches dotted internal module paths such as ``vfs.models.VersionMeta`` or
# ``vfs.errors.PermissionDeniedError``.  Replaced by the bare class name (last
# component) so the token remains informative but leaks no module structure.
_MODULE_PATH_RE = re.compile(r"\bvfs\.[\w.]+")


def _safe_error_message(exc: MontyError, inner: Exception | None) -> str:
    """Build a safe, host-path-free error message from a MontyError.

    Uses ``str(exc)`` (which Monty formats as ``"ExcType: message"``) but:
    1. Strips whitespace-delimited tokens that look like absolute host paths
       (start with ``/`` or ``~/``).
    2. Replaces dotted internal module paths (``vfs.models.VersionMeta``) with
       the bare class name only (``VersionMeta``), so adapter-internal names do
       not leak into the sandbox-visible error message.
    """
    raw = str(exc) if inner is None else f"{type(inner).__name__}: {inner}"
    # Strip tokens that look like absolute host paths.
    parts = raw.split()
    clean_parts = [p for p in parts if not p.startswith("/") and not p.startswith("~/")]
    msg = " ".join(clean_parts) if clean_parts else "Sandbox execution error"
    # Replace dotted internal module paths with their bare class name.
    msg = _MODULE_PATH_RE.sub(lambda m: m.group(0).rsplit(".", 1)[-1], msg)
    return msg
