"""Unit tests for the Session module."""

from __future__ import annotations

import pytest

from vfs.errors import PermissionDeniedError
from vfs.session import Session, resolve_path


class TestPublicApiSurface:
    """PublicApiSurface — Session and resolve_path are importable from the top-level vfs package."""

    def test_top_level_import(self):
        import vfs
        from vfs import Session as ExportedSession, resolve_path as exported_resolve_path

        assert ExportedSession is Session
        assert exported_resolve_path is resolve_path
        assert "Session" in vfs.__all__
        assert "resolve_path" in vfs.__all__


class TestResolvePath:
    """RelativePathResolution + PathTraversalPrevention — pure path utility."""

    def test_absolute_path_unchanged(self):
        assert resolve_path("/src/", "/data/file.txt") == "/data/file.txt"

    def test_relative_path_joined(self):
        assert resolve_path("/src/", "utils.py") == "/src/utils.py"

    def test_dot_path_resolved(self):
        assert resolve_path("/src/app/", "./config.py") == "/src/app/config.py"

    def test_dotdot_resolved(self):
        assert resolve_path("/src/app/", "../lib.py") == "/src/lib.py"

    def test_dotdot_at_root_clamped(self):
        assert resolve_path("/", "../etc/passwd") == "/etc/passwd"

    def test_deep_traversal_clamped(self):
        assert resolve_path("/workspace/", "../../../../etc/passwd") == "/etc/passwd"

    def test_double_slash_normalized(self):
        assert resolve_path("/src//", "file.py") == "/src/file.py"


class _MockVFS:
    """Minimal VFS stand-in for Session unit tests.

    Records permission checks and grants or denies them based on ``allow``.
    """

    def __init__(self, *, allow: bool = True) -> None:
        self.allow = allow
        self.check_calls: list[tuple[str, str, str, str]] = []

    async def _check_perm(self, principal_id: str, namespace_id: str, path: str, operation: str) -> None:
        self.check_calls.append((principal_id, namespace_id, path, operation))
        if not self.allow:
            raise PermissionDeniedError(f"denied: {path}")


class TestSessionConstruction:
    """CWDState — defaults and invariants."""

    def test_default_cwd(self):
        session = Session(_MockVFS(), "ns-1", "principal-1")
        assert session.pwd() == "/"

    @pytest.mark.asyncio
    async def test_cwd_always_absolute(self):
        """CWDIsAbsolute: pwd() begins with '/' across any sequence of cd ops, including failures."""
        denying = _MockVFS(allow=False)
        permissive = _MockVFS(allow=True)
        session = Session(permissive, "ns-1", "principal-1")
        assert session.pwd().startswith("/")

        for target in ["/workspace/", "src/", "./sub/", "../", "/", "..", "deep/nested/path/"]:
            await session.cd(target)
            assert session.pwd().startswith("/"), f"cwd lost absolute prefix after cd({target!r})"

        # A failed cd must not invalidate the invariant.
        session._vfs = denying
        with pytest.raises(PermissionDeniedError):
            await session.cd("/forbidden/")
        assert session.pwd().startswith("/")


class TestSessionPwd:
    """PwdOperation — reflects current cwd."""

    @pytest.mark.asyncio
    async def test_pwd_returns_cwd(self):
        vfs = _MockVFS()
        session = Session(vfs, "ns-1", "principal-1")
        await session.cd("/workspace/")
        assert session.pwd() == "/workspace/"


class TestSessionCd:
    """CdOperation — absolute, relative, dotdot, permission gating."""

    @pytest.mark.asyncio
    async def test_cd_absolute(self):
        vfs = _MockVFS()
        session = Session(vfs, "ns-1", "principal-1")
        await session.cd("/workspace/")
        assert session.pwd() == "/workspace/"

    @pytest.mark.asyncio
    async def test_cd_relative(self):
        vfs = _MockVFS()
        session = Session(vfs, "ns-1", "principal-1")
        await session.cd("/workspace/")
        await session.cd("src/")
        assert session.pwd() == "/workspace/src/"

    @pytest.mark.asyncio
    async def test_cd_dotdot(self):
        vfs = _MockVFS()
        session = Session(vfs, "ns-1", "principal-1")
        await session.cd("/workspace/src/")
        await session.cd("..")
        # cd normalizes targets as directory prefixes so scoped permission grants match.
        assert session.pwd() == "/workspace/"

    @pytest.mark.asyncio
    async def test_cd_at_root_stays_root(self):
        vfs = _MockVFS()
        session = Session(vfs, "ns-1", "principal-1")
        await session.cd("..")
        assert session.pwd() == "/"

    @pytest.mark.asyncio
    async def test_cd_permission_denied(self):
        vfs = _MockVFS(allow=False)
        session = Session(vfs, "ns-1", "principal-1")
        with pytest.raises(PermissionDeniedError):
            await session.cd("/secret/")
        assert session.pwd() == "/"

    @pytest.mark.asyncio
    async def test_cd_updates_only_on_success(self):
        """If the permission check raises, cwd MUST NOT have been mutated."""
        vfs = _MockVFS(allow=False)
        session = Session(vfs, "ns-1", "principal-1")
        # Move to a known location first via a permissive VFS.
        session._vfs = _MockVFS(allow=True)
        await session.cd("/workspace/")
        # Now swap to a denying VFS and attempt a failing cd.
        session._vfs = vfs
        with pytest.raises(PermissionDeniedError):
            await session.cd("/secret/")
        assert session.pwd() == "/workspace/"
        # And ensure the permission check happened before the (skipped) mutation:
        # the only recorded call is the failing one.
        assert vfs.check_calls == [("principal-1", "ns-1", "/secret/", "read")]
