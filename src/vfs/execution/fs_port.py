"""SessionFsPort — the session-backed FS-port implementation.

Every operation routes through the bound :class:`~vfs.session.Session`, so the
principal's permissions are enforced and state-changing operations are audited
exactly as for direct VFS calls. The host operating system's filesystem is never
touched. Operations with no VFS equivalent raise
:class:`~vfs.errors.UnsupportedOperationError`.

This is the single boundary both sandbox adapters (Monty ``AbstractOS``,
just-bash ``IFileSystem``) build on.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NoReturn

from vfs.errors import NotFoundError, UnsupportedOperationError
from vfs.protocols.fs_port import FsStat
from vfs.session import resolve_path

if TYPE_CHECKING:
    from vfs.session import Session


class SessionFsPort:
    """FS-port backed by a VFS :class:`~vfs.session.Session`.

    Relative paths are resolved through the session's ``cwd`` before delegating,
    consistent with the shell wrappers in :mod:`vfs.execution.fs_ops`.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def cwd(self) -> str:
        """The bound session's current working directory (for sandbox cwd alignment)."""
        return self._session.pwd()

    def _resolve(self, path: str) -> str:
        return resolve_path(self._session.pwd(), path)

    async def read(self, path: str) -> bytes:
        """Return the current content of ``path``."""
        return await self._session.read(self._resolve(path))

    async def write(self, path: str, data: bytes) -> int:
        """Write ``data`` (last-writer-wins) and return the new version number."""
        version_meta = await self._session.write(self._resolve(path), data)
        return version_meta.version_number

    async def list(self, path: str) -> list[str]:
        """Return the paths of the immediate children under ``path``."""
        resolved = self._resolve(path)
        prefix = resolved if resolved.endswith("/") else resolved + "/"
        metas = await self._session.list(prefix, recursive=False)
        return [m.path for m in metas]

    async def stat(self, path: str) -> FsStat:
        """Return an :class:`FsStat` for a file, or a synthesized directory prefix."""
        resolved = self._resolve(path)
        try:
            file_meta = await self._session.stat(resolved)
        except NotFoundError:
            # Not a file — it may be an implicit directory prefix (has children).
            prefix = resolved if resolved.endswith("/") else resolved + "/"
            children = await self._session.list(prefix, recursive=True)
            if children:
                return FsStat(size=0, is_dir=True, version_number=None)
            raise
        version = await self._session._vfs._meta.get_version(file_meta.namespace_id, file_meta.path)
        return FsStat(
            size=version.size if version is not None else 0,
            is_dir=False,
            version_number=file_meta.current_version_number,
        )

    async def exists(self, path: str) -> bool:
        """Return True if ``path`` is an existing file or a non-empty directory prefix."""
        try:
            await self.stat(path)
        except NotFoundError:
            return False
        return True

    async def delete(self, path: str) -> None:
        """Tombstone the file at ``path``."""
        await self._session.delete(self._resolve(path))

    async def mkdir(self, path: str) -> None:  # noqa: ARG002 — directories are implicit
        """No-op: directories are implicit prefixes in the VFS."""

    # --- Operations above the floor (no VFS equivalent) -----------------------

    def symlink(self, *args: object, **kwargs: object) -> NoReturn:
        """Symbolic links have no VFS equivalent."""
        self._unsupported("symlink")

    def readlink(self, *args: object, **kwargs: object) -> NoReturn:
        """Symbolic links have no VFS equivalent."""
        self._unsupported("readlink")

    def chmod(self, *args: object, **kwargs: object) -> NoReturn:
        """Permission modes have no VFS equivalent."""
        self._unsupported("chmod")

    def utime(self, *args: object, **kwargs: object) -> NoReturn:
        """Modification-time changes have no VFS equivalent."""
        self._unsupported("utime")

    @staticmethod
    def _unsupported(op: str) -> NoReturn:
        raise UnsupportedOperationError(f"Operation {op!r} has no VFS equivalent and is not supported by the FS-port.")
