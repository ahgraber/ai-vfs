"""Session — stateful CWD wrapper around a VFS for relative-path resolution."""

from __future__ import annotations

import posixpath
from typing import TYPE_CHECKING

from vfs.models import FullTextMatchMode

if TYPE_CHECKING:
    from vfs.models import FileMeta, SearchResult, SearchType, VersionMeta
    from vfs.protocols.execution import ExecutionResult, ResourceLimits
    from vfs.protocols.search import FindPredicates
    from vfs.vfs import VFS


def resolve_path(cwd: str, path: str) -> str:
    """Resolve a possibly-relative path against a CWD using POSIX semantics.

    Absolute paths pass through normalization only; relative paths are joined onto ``cwd`` first.
    ``posixpath.normpath`` collapses ``.``, ``..``, and duplicate slashes, and clamps
    traversal above ``/`` (``normpath("/../x") == "/x"``).

    A trailing ``/`` is preserved when the joined input ends with one, so directory-style
    arguments (e.g. ``"src/"``) reach the VFS as directory prefixes — required by ``list``
    and ``search``, which match files by string-prefix on the path.
    """
    joined = path if posixpath.isabs(path) else posixpath.join(cwd, path)
    result = posixpath.normpath(joined)
    if joined.endswith("/") and result != "/":
        result += "/"
    return result


class Session:
    """Stateful (cwd-bearing) facade over a VFS for a single principal in a single namespace.

    Path arguments may be absolute or relative; relative paths are resolved against ``cwd``
    via :func:`resolve_path` before delegating to the underlying VFS.
    """

    def __init__(self, vfs: VFS, namespace_id: str, principal_id: str) -> None:
        self._vfs = vfs
        self._namespace_id = namespace_id
        self._principal_id = principal_id
        self._cwd: str = "/"

    def pwd(self) -> str:
        """Return the current working directory."""
        return self._cwd

    async def cd(self, path: str) -> None:
        """Change cwd after verifying read permission on the resolved target.

        The resolved target is normalized as a directory prefix (always trailing ``/``,
        except for root) so it both matches the ``CdDotDot`` spec scenario and aligns
        with permission grants that store directory-style ``path_prefix`` values.
        ``cwd`` is updated only if the permission check succeeds; on denial,
        ``PermissionDeniedError`` propagates and ``cwd`` is unchanged.
        """
        resolved = resolve_path(self._cwd, path)
        if not resolved.endswith("/"):
            resolved += "/"
        await self._vfs._check_perm(self._principal_id, self._namespace_id, resolved, "read")
        self._cwd = resolved

    # --- VFS proxies — each resolves path args through cwd before delegating ---

    async def stat(self, path: str) -> FileMeta:
        """Return file metadata for ``path`` (resolved through ``cwd``)."""
        return await self._vfs.stat(self._namespace_id, resolve_path(self._cwd, path), principal_id=self._principal_id)

    async def list(self, path_prefix: str, *, recursive: bool = False) -> list[FileMeta]:
        """List files under ``path_prefix`` (resolved through ``cwd``)."""
        return await self._vfs.list(
            self._namespace_id,
            resolve_path(self._cwd, path_prefix),
            principal_id=self._principal_id,
            recursive=recursive,
        )

    async def read(self, path: str, *, version_number: int | None = None) -> bytes:
        """Read content of ``path`` (resolved through ``cwd``)."""
        return await self._vfs.read(
            self._namespace_id,
            resolve_path(self._cwd, path),
            principal_id=self._principal_id,
            version_number=version_number,
        )

    async def write(
        self,
        path: str,
        content: bytes,
        *,
        expected_version: int | None = None,
    ) -> VersionMeta:
        """Write ``content`` to ``path`` (resolved through ``cwd``)."""
        return await self._vfs.write(
            self._namespace_id,
            resolve_path(self._cwd, path),
            content,
            principal_id=self._principal_id,
            expected_version=expected_version,
        )

    async def delete(self, path: str) -> VersionMeta:
        """Tombstone ``path`` (resolved through ``cwd``)."""
        return await self._vfs.delete(
            self._namespace_id, resolve_path(self._cwd, path), principal_id=self._principal_id
        )

    async def copy(
        self,
        src: str,
        dst: str,
        *,
        expected_version: int | None = None,
    ) -> VersionMeta:
        """Copy ``src`` to ``dst`` (both resolved through ``cwd``)."""
        return await self._vfs.copy(
            self._namespace_id,
            resolve_path(self._cwd, src),
            resolve_path(self._cwd, dst),
            principal_id=self._principal_id,
            expected_version=expected_version,
        )

    async def move(self, src: str, dst: str) -> VersionMeta:
        """Move ``src`` to ``dst`` (both resolved through ``cwd``)."""
        return await self._vfs.move(
            self._namespace_id,
            resolve_path(self._cwd, src),
            resolve_path(self._cwd, dst),
            principal_id=self._principal_id,
        )

    async def versions(
        self,
        path: str,
        *,
        limit: int = 50,
        before: int | None = None,
    ) -> list[VersionMeta]:
        """Return version history for ``path`` (resolved through ``cwd``)."""
        return await self._vfs.versions(
            self._namespace_id,
            resolve_path(self._cwd, path),
            principal_id=self._principal_id,
            limit=limit,
            before=before,
        )

    async def rollback(self, path: str, target_version: int) -> VersionMeta:
        """Roll ``path`` (resolved through ``cwd``) back to ``target_version``."""
        return await self._vfs.rollback(
            self._namespace_id,
            resolve_path(self._cwd, path),
            target_version,
            principal_id=self._principal_id,
        )

    async def search(
        self,
        query: str,
        scope: str,
        search_type: SearchType,
        *,
        find_predicates: FindPredicates | None = None,
        match_mode: FullTextMatchMode = FullTextMatchMode.ALL,
    ) -> list[SearchResult]:
        """Search ``scope`` (resolved through ``cwd``) for ``query``.

        ``find_predicates`` is forwarded to the underlying ``vfs.search`` call unchanged.
        ``match_mode`` applies only to ``FULLTEXT`` searches (``ALL`` = strict-AND,
        ``ANY`` = ranked-OR) and is ignored for GLOB, FIND, and REGEX; it is forwarded
        to ``vfs.search`` unchanged.
        """
        return await self._vfs.search(
            self._namespace_id,
            query,
            resolve_path(self._cwd, scope),
            search_type,
            principal_id=self._principal_id,
            find_predicates=find_predicates,
            match_mode=match_mode,
        )

    async def execute(
        self,
        code: str,
        provider_name: str,
        *,
        timeout: float | None = None,
        resource_limits: ResourceLimits | None = None,
    ) -> ExecutionResult:
        """Execute ``code`` in the sandbox bound to this session's namespace, principal, and cwd.

        Delegates to :meth:`~vfs.vfs.VFS.execute` with the session's ``namespace_id``,
        ``principal_id``, and current ``cwd``.  The ``execute`` permission check is
        performed inside ``vfs.execute``, not here.
        """
        return await self._vfs.execute(
            code,
            self._namespace_id,
            self._principal_id,
            provider_name,
            timeout=timeout,
            resource_limits=resource_limits,
            cwd=self._cwd,
        )
