"""Integration tests for Session proxy operations against a real VFS."""

from __future__ import annotations

import pytest

from vfs.models import SearchType
from vfs.session import Session


async def _setup_session(vfs, *, cwd: str = "/workspace") -> tuple[Session, str, str]:
    """Bootstrap a namespace + principal with full perms; return (session, ns_id, principal_id).

    Optionally pre-positions the session at ``cwd``.
    """
    ns = await vfs.create_namespace("test-ws", "admin")
    admin = await vfs.create_principal("test-admin")
    await vfs.bootstrap_admin(admin.id, ns.id)
    p = await vfs.create_principal("agent")
    await vfs.grant(admin.id, p.id, ns.id, "/", {"read", "write", "delete"})
    session = Session(vfs, ns.id, p.id)
    if cwd != "/":
        await session.cd(cwd)
    return session, ns.id, p.id


class TestSessionProxiesVFS:
    """SessionProxiesVFS — every proxy method resolves path args through cwd before delegating."""

    @pytest.mark.asyncio
    async def test_session_read_relative(self, vfs_instance):
        session, ns_id, p_id = await _setup_session(vfs_instance)
        await vfs_instance.write(ns_id, "/workspace/file.txt", b"hello", principal_id=p_id)
        content = await session.read("file.txt")
        assert content == b"hello"

    @pytest.mark.asyncio
    async def test_session_write_relative(self, vfs_instance):
        session, ns_id, p_id = await _setup_session(vfs_instance)
        await session.write("output.txt", b"data")
        meta = await vfs_instance.stat(ns_id, "/workspace/output.txt", principal_id=p_id)
        assert meta.path == "/workspace/output.txt"

    @pytest.mark.asyncio
    async def test_session_list_relative(self, vfs_instance):
        session, ns_id, p_id = await _setup_session(vfs_instance)
        for path in ("/workspace/src/a.py", "/workspace/src/b.py"):
            await vfs_instance.write(ns_id, path, b"x", principal_id=p_id)
        results = await session.list("src/")
        paths = {r.path for r in results}
        assert paths == {"/workspace/src/a.py", "/workspace/src/b.py"}

    @pytest.mark.asyncio
    async def test_session_stat_relative(self, vfs_instance):
        session, ns_id, p_id = await _setup_session(vfs_instance)
        await vfs_instance.write(ns_id, "/workspace/src/main.py", b"x", principal_id=p_id)
        meta = await session.stat("src/main.py")
        assert meta.path == "/workspace/src/main.py"

    @pytest.mark.asyncio
    async def test_session_delete_relative(self, vfs_instance):
        session, ns_id, p_id = await _setup_session(vfs_instance)
        await vfs_instance.write(ns_id, "/workspace/old.txt", b"x", principal_id=p_id)
        await session.delete("old.txt")
        meta = await vfs_instance.stat(ns_id, "/workspace/old.txt", principal_id=p_id)
        assert meta.is_deleted is True

    @pytest.mark.asyncio
    async def test_session_copy_relative_both(self, vfs_instance):
        session, ns_id, p_id = await _setup_session(vfs_instance)
        await vfs_instance.write(ns_id, "/workspace/a.py", b"data", principal_id=p_id)
        await session.copy("a.py", "../archive/a.py")
        # Source unchanged at /workspace/a.py; dest at /archive/a.py
        src_meta = await vfs_instance.stat(ns_id, "/workspace/a.py", principal_id=p_id)
        assert src_meta.is_deleted is False
        dst_content = await vfs_instance.read(ns_id, "/archive/a.py", principal_id=p_id)
        assert dst_content == b"data"

    @pytest.mark.asyncio
    async def test_session_move_relative_both(self, vfs_instance):
        session, ns_id, p_id = await _setup_session(vfs_instance)
        await vfs_instance.write(ns_id, "/workspace/a.py", b"data", principal_id=p_id)
        await session.move("a.py", "../archive/a.py")
        src_meta = await vfs_instance.stat(ns_id, "/workspace/a.py", principal_id=p_id)
        assert src_meta.is_deleted is True
        dst_content = await vfs_instance.read(ns_id, "/archive/a.py", principal_id=p_id)
        assert dst_content == b"data"

    @pytest.mark.asyncio
    async def test_session_search_relative(self, vfs_instance):
        session, ns_id, p_id = await _setup_session(vfs_instance)
        await vfs_instance.write(ns_id, "/workspace/src/main.py", b"x", principal_id=p_id)
        await vfs_instance.write(ns_id, "/workspace/src/util.py", b"x", principal_id=p_id)
        await vfs_instance.write(ns_id, "/workspace/other/note.txt", b"x", principal_id=p_id)
        results = await session.search("*.py", "src/", SearchType.GLOB)
        paths = {r.path for r in results}
        assert paths == {"/workspace/src/main.py", "/workspace/src/util.py"}

    @pytest.mark.asyncio
    async def test_session_versions_relative(self, vfs_instance):
        session, ns_id, p_id = await _setup_session(vfs_instance)
        await vfs_instance.write(ns_id, "/workspace/file.txt", b"v1", principal_id=p_id)
        await vfs_instance.write(ns_id, "/workspace/file.txt", b"v2", principal_id=p_id)
        history = await session.versions("file.txt")
        assert {v.version_number for v in history} == {1, 2}
        assert all(v.file_path == "/workspace/file.txt" for v in history)

    @pytest.mark.asyncio
    async def test_session_rollback_relative(self, vfs_instance):
        session, ns_id, p_id = await _setup_session(vfs_instance)
        await vfs_instance.write(ns_id, "/workspace/file.txt", b"v1", principal_id=p_id)
        await vfs_instance.write(ns_id, "/workspace/file.txt", b"v2", principal_id=p_id)
        new_ver = await session.rollback("file.txt", 1)
        assert new_ver.file_path == "/workspace/file.txt"
        assert new_ver.version_number == 3
        content = await vfs_instance.read(ns_id, "/workspace/file.txt", principal_id=p_id)
        assert content == b"v1"


class TestSessionCdScopedGrant:
    """CdDotDot under a directory-prefix grant — cd must produce a cwd that the
    permission check (``path.startswith(prefix)``) actually matches.
    """

    @pytest.mark.asyncio
    async def test_cd_dotdot_under_scoped_grant(self, vfs_instance):
        ns = await vfs_instance.create_namespace("scoped-ws", "admin")
        admin = await vfs_instance.create_principal("scoped-admin")
        await vfs_instance.bootstrap_admin(admin.id, ns.id)
        p = await vfs_instance.create_principal("scoped-agent")
        # Grant scoped strictly to the workspace prefix (with trailing slash) — not root.
        await vfs_instance.grant(admin.id, p.id, ns.id, "/workspace/", {"read", "write", "delete"})

        session = Session(vfs_instance, ns.id, p.id)
        await session.cd("/workspace/src/")
        assert session.pwd() == "/workspace/src/"

        # cd("..") must produce a cwd that the /workspace/ grant covers; otherwise
        # _check_perm raises PermissionDeniedError.
        await session.cd("..")
        assert session.pwd() == "/workspace/"
