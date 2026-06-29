"""FS-port protocol: the boundary between the VFS layer and execution sandboxes.

The FS-port is the **weakest common denominator** of what the Monty
``AbstractOS`` mount and the just-bash ``IFileSystem`` need, and what the VFS
``Session`` can govern: an async, whole-file, path-based filesystem interface.
Every operation routes through a bound ``Session``, so permissions are enforced
and state-changing operations are audited exactly as for direct VFS calls. It
exposes no streaming and no host filesystem.

Operations above the floor — symbolic links, permission-mode changes,
modification-time changes — have no VFS equivalent and raise
:class:`~vfs.errors.UnsupportedOperationError` rather than silently succeeding.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class FsStat:
    """Minimal stat record at the FS-port floor.

    ``version_number`` is the file's current VFS version; it is ``None`` for a
    synthesized directory prefix (the VFS has no directory objects).
    """

    size: int
    is_dir: bool
    version_number: int | None


@runtime_checkable
class FsPort(Protocol):
    """Async, whole-file, path-based filesystem interface backed by a Session."""

    async def read(self, path: str) -> bytes:
        """Return the current content of ``path`` (raises ``NotFoundError`` if absent)."""
        ...

    async def write(self, path: str, data: bytes) -> int:
        """Write ``data`` to ``path`` (last-writer-wins) and return the new version number."""
        ...

    async def list(self, path: str) -> list[str]:
        """Return the paths of the immediate children under ``path``."""
        ...

    async def stat(self, path: str) -> FsStat:
        """Return an :class:`FsStat` for a file or a synthesized directory prefix."""
        ...

    async def exists(self, path: str) -> bool:
        """Return True if ``path`` is an existing file or a non-empty directory prefix."""
        ...

    async def delete(self, path: str) -> None:
        """Tombstone the file at ``path``."""
        ...

    async def mkdir(self, path: str) -> None:
        """No-op: directories are implicit prefixes in the VFS."""
        ...
