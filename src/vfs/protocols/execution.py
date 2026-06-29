"""ExecutionProvider protocol and supporting data types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class ExecutionResult:
    """Result returned by an execution provider.

    ``success=True`` means the code ran to completion; ``output`` holds the value.
    ``success=False`` means a structured failure; ``error_type`` identifies the
    failure class and ``error_message`` carries an actionable string (no host paths
    or raw tracebacks).
    """

    success: bool
    output: Any = None
    error_type: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class ExecutionCapabilities:
    """Description of an execution provider's declared capabilities.

    ``supports_async`` indicates whether the provider can await coroutines from
    external functions on the host event loop.  ``language`` names the scripting
    language (e.g. ``"python"``).  ``tier`` is a short identifier (e.g. ``"monty"``).
    """

    supports_async: bool
    language: str
    tier: str


@dataclass
class ResourceLimits:
    """Per-execution resource limits.

    ``timeout_seconds`` is the end-to-end wall-clock budget enforced by
    ``vfs.execute`` via ``asyncio.wait_for``; providers may use it as an inner
    secondary limit too.  ``max_operations`` caps the number of VFS callbacks
    (one call to any shell wrapper = one operation); the ``OperationCounter`` in
    ``fs_operations_for`` enforces this.  ``max_read_bytes`` caps the content
    returned by a single ``cat``/``head``/``tail`` call; an oversized file yields
    a structured error rather than a host OOM.  ``max_result_items`` caps items
    returned by ``grep``/``find``/``ls``.  ``None`` means unlimited.
    """

    timeout_seconds: float = 30.0
    max_memory_bytes: int | None = None
    max_operations: int = 1000
    max_read_bytes: int | None = None
    max_result_items: int | None = None


@runtime_checkable
class ExecutionProvider(Protocol):
    """Protocol for sandboxed code execution providers.

    An ``ExecutionProvider`` is stateless across executions: each ``execute``
    call receives a fresh ``FsOperations`` and ``ResourceLimits``.  ``reset``
    clears any provider-level state (e.g. warm-up caches); it is a no-op for
    stateless providers.
    """

    async def execute(
        self,
        code: str,
        fs_ops: Any,
        fs_port: Any,
        resource_limits: ResourceLimits,
    ) -> ExecutionResult:
        """Run ``code`` inside the sandbox over the governed VFS.

        ``fs_ops`` is the session-bound :class:`~vfs.execution.fs_ops.FsOperations`
        instance (the injected verbs); ``fs_port`` is the session-backed
        :class:`~vfs.protocols.fs_port.FsPort` a provider mounts as the sandbox's
        native filesystem. Both are constructed by ``vfs.execute``. The end-to-end
        timeout is enforced by the caller (``asyncio.wait_for``); providers may
        additionally use ``resource_limits.timeout_seconds`` as an inner limit.
        Returns :class:`ExecutionResult` — never raises for execution-time errors.
        """
        ...

    def capabilities(self) -> ExecutionCapabilities:
        """Return a description of this provider's capabilities."""
        ...

    def reset(self) -> None:
        """Reset any provider-level state.  No-op for stateless providers."""
        ...
