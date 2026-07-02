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

from vfs.errors import NotFoundError, ResourceLimitExceededError, UnsupportedOperationError
from vfs.protocols.fs_port import FsStat
from vfs.session import resolve_path

if TYPE_CHECKING:
    from vfs.execution.fs_ops import OperationCounter
    from vfs.protocols.execution import ResourceLimits
    from vfs.session import Session


class SessionFsPort:
    """FS-port backed by a VFS :class:`~vfs.session.Session`.

    Relative paths are resolved through the session's ``cwd`` before delegating,
    consistent with the shell wrappers in :mod:`vfs.execution.fs_ops`.

    Resource governance
    -------------------
    This is the surface a sandbox mounts as its native filesystem (Monty
    ``AbstractOS``, just-bash ``IFileSystem``), so it — not just the injected
    ``FsOperations`` verbs — must enforce ``ResourceLimits``.  Each operation is
    charged against the shared ``OperationCounter`` (the ``max_operations``
    budget), reads are refused when the target exceeds ``max_read_bytes``
    (checked via ``stat`` before the blob is fetched), and writes are refused
    when the payload exceeds ``max_write_bytes``.  ``resource_limits`` /
    ``counter`` default to ``None`` (no enforcement) for direct/trusted
    construction; ``vfs.execute`` always supplies both for sandboxed runs.
    """

    def __init__(
        self,
        session: Session,
        resource_limits: ResourceLimits | None = None,
        counter: OperationCounter | None = None,
    ) -> None:
        self._session = session
        self._limits = resource_limits
        self._counter = counter

    @property
    def cwd(self) -> str:
        """The bound session's current working directory (for sandbox cwd alignment)."""
        return self._session.pwd()

    def _resolve(self, path: str) -> str:
        return resolve_path(self._session.pwd(), path)

    def _account(self) -> None:
        """Charge one operation against the shared budget (raises when exhausted)."""
        if self._counter is not None:
            self._counter.check_and_increment()

    async def _enforce_read_size(self, resolved: str) -> None:
        """Raise if the file at ``resolved`` exceeds ``max_read_bytes``.

        Uses ``stat`` (which enforces read permission and raises ``NotFoundError``
        when absent) plus the stored ``VersionMeta.size`` so an oversized blob is
        never fetched into host memory.
        """
        if self._limits is None or self._limits.max_read_bytes is None:
            return
        file_meta = await self._session.stat(resolved)
        ver = await self._session._vfs._meta.get_version(file_meta.namespace_id, file_meta.path)
        if ver is not None and ver.size > self._limits.max_read_bytes:
            raise ResourceLimitExceededError(
                f"File exceeds max_read_bytes limit ({self._limits.max_read_bytes} bytes)"
            )

    def _enforce_write_size(self, data: bytes) -> None:
        """Raise if ``data`` exceeds ``max_write_bytes``."""
        if self._limits is None or self._limits.max_write_bytes is None:
            return
        if len(data) > self._limits.max_write_bytes:
            raise ResourceLimitExceededError(
                f"Write exceeds max_write_bytes limit ({self._limits.max_write_bytes} bytes)"
            )

    async def read(self, path: str) -> bytes:
        """Return the current content of ``path`` (refused when over ``max_read_bytes``)."""
        self._account()
        resolved = self._resolve(path)
        await self._enforce_read_size(resolved)
        return await self._session.read(resolved)

    async def write(self, path: str, data: bytes) -> int:
        """Write ``data`` (last-writer-wins) and return the new version number.

        Refused when ``data`` exceeds ``max_write_bytes``.
        """
        self._account()
        self._enforce_write_size(data)
        version_meta = await self._session.write(self._resolve(path), data)
        return version_meta.version_number

    async def list(self, path: str) -> list[str]:
        """Return the paths of the immediate children under ``path``."""
        self._account()
        return await self._list(self._resolve(path))

    async def _list(self, resolved: str) -> list[str]:
        prefix = resolved if resolved.endswith("/") else resolved + "/"
        metas = await self._session.list(prefix, recursive=False)
        return [m.path for m in metas]

    async def stat(self, path: str) -> FsStat:
        """Return an :class:`FsStat` for a file, or a synthesized directory prefix."""
        self._account()
        return await self._stat(self._resolve(path))

    async def _stat(self, resolved: str) -> FsStat:
        try:
            file_meta = await self._session.stat(resolved)
        except NotFoundError:
            # Not a file — it may be an implicit directory prefix (has children).
            # TODO(perf): a ``has_descendants(prefix)`` store method would replace
            # this recursive scan (hit only on the not-a-file path) with a LIMIT 1.
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
        self._account()
        try:
            await self._stat(self._resolve(path))
        except NotFoundError:
            return False
        return True

    async def delete(self, path: str) -> None:
        """Tombstone the file at ``path``."""
        self._account()
        await self._session.delete(self._resolve(path))

    async def mkdir(self, path: str) -> None:  # noqa: ARG002 — directories are implicit
        """No-op (directories are implicit prefixes); charged against the budget."""
        self._account()

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
