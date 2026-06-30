"""MontyVfsOS — mount the FS-port as Monty's native filesystem.

This is an **interpreter-level virtual filesystem**: a proxy into the governed
VFS, not an OS/FUSE mount. The host operating-system filesystem is never
attached or exposed to the sandbox. Sandboxed code's ``open``/``pathlib``/``os``
path operations are intercepted by Monty and routed here, where each call drives
the asynchronous FS-port — and therefore the VFS's permission and audit
enforcement.

Monty dispatches these filesystem callbacks **off** the host event-loop thread
(spike-verified: ``same_thread = False``). Each synchronous callback bridges to
the async FS-port via ``asyncio.run_coroutine_threadsafe(coro, host_loop).result()``,
which blocks only the calling worker thread while the coroutine runs on the host
loop — no re-entrancy and no loop starvation.

A VFS error raised inside a bridged callback (``PermissionDeniedError``,
``NotFoundError``, …) is captured in a shared sentinel so the provider can
re-raise it after Monty's exception downcast, letting ``vfs.execute``'s
translation table map it to its real ``error_type`` rather than a generic
provider/internal error.
"""

from __future__ import annotations

import asyncio
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from pydantic_monty import AbstractOS, MontyFileHandle, StatResult
from pydantic_monty.os_access import path_from_arg

from vfs.errors import UnsupportedOperationError, VFSError

if TYPE_CHECKING:
    from vfs.protocols.fs_port import FsPort


class MontyVfsOS(AbstractOS):
    """Adapter mapping Monty's ``Path.*``/``Open``/``os`` operations onto the FS-port."""

    def __init__(
        self,
        fs_port: FsPort,
        loop: asyncio.AbstractEventLoop,
        error_sink: list[VFSError],
    ) -> None:
        self._fs = fs_port
        self._loop = loop
        self._error_sink = error_sink

    # --- sync→async bridge -----------------------------------------------------

    def _await(self, coro: Any) -> Any:
        """Drive an async FS-port coroutine to completion from a worker thread.

        Captures the first VFS error so the provider can re-raise it with its
        identity intact after Monty downcasts the exception.
        """
        try:
            return asyncio.run_coroutine_threadsafe(coro, self._loop).result()
        except VFSError as exc:
            if not self._error_sink:
                self._error_sink.append(exc)
            raise

    @staticmethod
    def _path(arg: PurePosixPath | MontyFileHandle) -> str:
        return str(path_from_arg(arg))

    # --- queries ---------------------------------------------------------------

    def path_exists(self, path: PurePosixPath) -> bool:
        """Return True if ``path`` is an existing VFS file or directory prefix."""
        return self._await(self._fs.exists(str(path)))

    def path_is_file(self, path: PurePosixPath) -> bool:
        """Return True if ``path`` is an existing VFS file."""
        if not self._await(self._fs.exists(str(path))):
            return False
        return not self._await(self._fs.stat(str(path))).is_dir

    def path_is_dir(self, path: PurePosixPath) -> bool:
        """Return True if ``path`` is an implicit VFS directory prefix."""
        if not self._await(self._fs.exists(str(path))):
            return False
        return self._await(self._fs.stat(str(path))).is_dir

    def path_is_symlink(self, path: PurePosixPath) -> bool:  # noqa: ARG002 — VFS has no symlinks
        """Return False: the VFS has no symbolic links."""
        return False

    def path_stat(self, path: PurePosixPath) -> StatResult:
        """Return a Monty ``StatResult`` for a VFS file or directory prefix."""
        st = self._await(self._fs.stat(str(path)))
        return StatResult.dir_stat() if st.is_dir else StatResult.file_stat(size=st.size)

    def path_iterdir(self, path: PurePosixPath) -> list[PurePosixPath]:
        """Return the immediate children under ``path``."""
        return [PurePosixPath(child) for child in self._await(self._fs.list(str(path)))]

    # --- reads -----------------------------------------------------------------

    def path_read_bytes(self, path: PurePosixPath | MontyFileHandle) -> bytes:
        """Return the current bytes of ``path``."""
        return self._await(self._fs.read(self._path(path)))

    def path_read_text(self, path: PurePosixPath | MontyFileHandle) -> str:
        """Return the current content of ``path`` decoded as UTF-8."""
        return self._await(self._fs.read(self._path(path))).decode("utf-8")

    # --- writes ----------------------------------------------------------------

    def path_write_bytes(self, path: PurePosixPath | MontyFileHandle, data: bytes) -> int:
        """Write ``data`` to ``path`` (last-writer-wins); return the byte count."""
        self._await(self._fs.write(self._path(path), data))
        return len(data)

    def path_write_text(self, path: PurePosixPath | MontyFileHandle, data: str) -> int:
        """Write ``data`` as UTF-8 to ``path``; return the character count."""
        encoded = data.encode("utf-8")
        self._await(self._fs.write(self._path(path), encoded))
        return len(data)

    def path_append_text(self, path: PurePosixPath | MontyFileHandle, data: str) -> int:
        """Append UTF-8 ``data`` to ``path``; return the character count.

        Monty routes writes to an ``open(path, 'w'|'a')`` handle through append
        (the open-time effect truncated/created the file). The whole-file FS-port
        has no append primitive, so this reads the current bytes and rewrites.
        """
        self._append(self._path(path), data.encode("utf-8"))
        return len(data)

    def path_append_bytes(self, path: PurePosixPath | MontyFileHandle, data: bytes) -> int:
        """Append ``data`` bytes to ``path``; return the byte count."""
        self._append(self._path(path), data)
        return len(data)

    def _append(self, path: str, data: bytes) -> None:
        from vfs.errors import NotFoundError

        try:
            existing = self._await(self._fs.read(path))
        except NotFoundError:
            existing = b""
        self._await(self._fs.write(path, existing + data))

    def path_open(self, path: PurePosixPath, mode: str) -> MontyFileHandle:
        """Perform the ``open(path, mode)`` open-time effect against the VFS.

        ``r`` verifies the file exists; ``w`` truncates/creates; ``a`` creates if
        missing. Permission and not-found errors propagate through the bridge.
        """
        handle = MontyFileHandle(str(path), mode)
        action = handle.mode[0]
        empty = b"" if handle.binary else ""
        if action == "r":
            if not self._await(self._fs.exists(str(path))):
                raise FileNotFoundError(f"[Errno 2] No such file or directory: {str(path)!r}")
        elif action == "w":
            self._await(self._fs.write(str(path), b""))
        else:  # 'a' — create if missing, leave existing content untouched
            if not self._await(self._fs.exists(str(path))):
                self._await(self._fs.write(str(path), b""))
        _ = empty
        return handle

    # --- mutating directory / namespace ops -----------------------------------

    def path_mkdir(self, path: PurePosixPath, parents: bool, exist_ok: bool) -> None:  # noqa: ARG002
        """Create ``path`` (a no-op in the VFS, where directories are implicit prefixes)."""
        self._await(self._fs.mkdir(str(path)))

    def path_rmdir(self, path: PurePosixPath) -> None:  # noqa: ARG002 — directories are implicit
        """No-op: directories are implicit prefixes; removing one removes nothing."""

    def path_unlink(self, path: PurePosixPath) -> None:
        """Tombstone the file at ``path``."""
        self._await(self._fs.delete(str(path)))

    def path_rename(self, path: PurePosixPath, target: PurePosixPath) -> None:  # noqa: ARG002
        """Reject rename: unsupported by the FS-port mount."""
        raise UnsupportedOperationError(
            "rename is not supported by the FS-port mount; use the VFS move API outside the sandbox."
        )

    # --- path normalization ----------------------------------------------------

    def path_resolve(self, path: PurePosixPath) -> str:
        """Resolve ``path`` to an absolute POSIX path."""
        return self.path_absolute(path)

    def path_absolute(self, path: PurePosixPath) -> str:
        """Return ``path`` as an absolute POSIX path."""
        p = PurePosixPath(path)
        return str(p if p.is_absolute() else PurePosixPath("/") / p)

    # --- environment isolation -------------------------------------------------

    def getenv(self, key: str, default: str | None = None) -> str | None:  # noqa: ARG002
        """Return ``default``: the sandbox sees no host environment."""
        return default

    def get_environ(self) -> dict[str, str]:
        """Return an empty environment: the host environment is not exposed."""
        return {}
