"""Unit tests for the session-backed FS-port.

Covers `FsPortContract`:
  FsPortReadWriteRouteThroughSession
  FsPortRejectsUnauthorizedPath
  FsPortMkdirIsNoOp
  FsPortUnsupportedOperationRaises
  (+ host filesystem is not reachable through the port)
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from vfs.config import VFSConfig
from vfs.errors import (
    NotFoundError,
    OperationBudgetExceededError,
    PermissionDeniedError,
    ResourceLimitExceededError,
    UnsupportedOperationError,
)
from vfs.execution.fs_ops import OperationCounter, fs_operations_for
from vfs.execution.fs_port import SessionFsPort
from vfs.protocols.execution import ResourceLimits
from vfs.protocols.fs_port import FsStat
from vfs.session import Session
from vfs.vfs import VFS


@pytest_asyncio.fixture
async def vfs_inst(tmp_path):
    config = VFSConfig(
        metadata_store_uri=f"sqlite:///{tmp_path / 'test.db'}",
        blob_store_uri=f"file:///{tmp_path / 'blobs'}/",
        otel_enabled=False,
        audit_log_enabled=False,
        blob_cache_enabled=False,
    )
    v = VFS(config)
    await v.initialize()
    yield v
    await v.close()


@pytest_asyncio.fixture
async def env(vfs_inst):
    """Namespace + full-access principal; returns (vfs, ns, admin, agent, port)."""
    ns = await vfs_inst.create_namespace("testns", "admin")
    admin = await vfs_inst.create_principal("admin-user")
    await vfs_inst.bootstrap_admin(admin.id, ns.id)
    agent = await vfs_inst.create_principal("agent-user")
    await vfs_inst.grant(admin.id, agent.id, ns.id, "/", {"read", "write", "delete"})
    port = SessionFsPort(Session(vfs_inst, ns.id, agent.id))
    return vfs_inst, ns, admin, agent, port


class TestFsPortContract:
    @pytest.mark.asyncio
    async def test_read_write_route_through_session(self, env):
        _, _, _, _, port = env
        version = await port.write("/a.txt", b"hello")
        assert version == 1
        assert await port.read("/a.txt") == b"hello"
        st = await port.stat("/a.txt")
        assert isinstance(st, FsStat)
        assert st.size == 5 and st.is_dir is False and st.version_number == 1
        assert await port.exists("/a.txt") is True
        assert await port.exists("/missing.txt") is False

    @pytest.mark.asyncio
    async def test_rejects_unauthorized_path(self, vfs_inst):
        ns = await vfs_inst.create_namespace("ns2", "admin")
        admin = await vfs_inst.create_principal("admin2")
        await vfs_inst.bootstrap_admin(admin.id, ns.id)
        author = await vfs_inst.create_principal("author")
        await vfs_inst.grant(admin.id, author.id, ns.id, "/", {"read", "write"})
        await SessionFsPort(Session(vfs_inst, ns.id, author.id)).write("/secret.txt", b"x")
        # A principal granted only under /pub cannot read /secret.txt.
        outsider = await vfs_inst.create_principal("outsider")
        await vfs_inst.grant(admin.id, outsider.id, ns.id, "/pub/", {"read"})
        port = SessionFsPort(Session(vfs_inst, ns.id, outsider.id))
        with pytest.raises(PermissionDeniedError):
            await port.read("/secret.txt")

    @pytest.mark.asyncio
    async def test_mkdir_is_noop_then_write_under_prefix(self, env):
        _, _, _, _, port = env
        await port.mkdir("/sub")  # no directory entity created
        # Listing the (empty) prefix yields nothing; writing under it still works.
        assert await port.list("/sub") == []
        await port.write("/sub/file.txt", b"data")
        children = await port.list("/sub")
        assert children == ["/sub/file.txt"]
        # The prefix now stats as a synthesized directory.
        st = await port.stat("/sub")
        assert st.is_dir is True and st.version_number is None

    @pytest.mark.asyncio
    async def test_unsupported_operations_raise(self, env):
        _, _, _, _, port = env
        with pytest.raises(UnsupportedOperationError):
            port.symlink("/a", "/b")
        with pytest.raises(UnsupportedOperationError):
            port.readlink("/a")
        with pytest.raises(UnsupportedOperationError):
            port.chmod("/a", 0o644)
        with pytest.raises(UnsupportedOperationError):
            port.utime("/a", (0, 0))

    @pytest.mark.asyncio
    async def test_host_filesystem_not_reachable(self, env):
        """An absolute path that exists on the host is not reachable via the port."""
        _, _, _, _, port = env
        # /etc/hosts exists on the host but is not a VFS path → NotFoundError, not host bytes.
        with pytest.raises(NotFoundError):
            await port.read("/etc/hosts")


class TestFsPortResourceGovernance:
    """The native mount enforces ResourceLimits: read/write size caps and the shared budget.

    Guards against the native-mount OOM/budget-bypass: ``open().read()`` /
    redirection route through this port, not the injected verbs, so it — not only
    ``FsOperations`` — must enforce the caps.
    """

    @pytest.mark.asyncio
    async def test_read_over_max_read_bytes_is_refused_without_fetching(self, env):
        vfs_inst, ns, _, agent, _ = env
        session = Session(vfs_inst, ns.id, agent.id)
        await SessionFsPort(session).write("/big.txt", b"x" * 100)
        limited = SessionFsPort(session, ResourceLimits(max_read_bytes=10))
        with pytest.raises(ResourceLimitExceededError):
            await limited.read("/big.txt")

    @pytest.mark.asyncio
    async def test_write_over_max_write_bytes_is_refused(self, env):
        vfs_inst, ns, _, agent, _ = env
        port = SessionFsPort(Session(vfs_inst, ns.id, agent.id), ResourceLimits(max_write_bytes=8))
        with pytest.raises(ResourceLimitExceededError):
            await port.write("/big.txt", b"x" * 9)

    @pytest.mark.asyncio
    async def test_native_mount_and_verbs_share_one_operation_budget(self, env):
        vfs_inst, ns, _, agent, _ = env
        session = Session(vfs_inst, ns.id, agent.id)
        # A budget of 2 shared between the injected verbs and the port: the port
        # spends one on write, fs_ops spends the second on cat, the third is refused.
        limits = ResourceLimits(max_operations=2)
        counter = OperationCounter(limits.max_operations)
        fs_ops = fs_operations_for(session, limits, counter)
        port = SessionFsPort(session, limits, counter)
        await port.write("/a.txt", b"hi")  # op 1 (via the mount)
        await fs_ops.cat("/a.txt")  # op 2 (via a verb)
        with pytest.raises(OperationBudgetExceededError):
            await port.read("/a.txt")  # op 3 — over the shared budget

    @pytest.mark.asyncio
    async def test_no_limits_means_no_enforcement(self, env):
        """Direct construction without limits/counter keeps the port unrestricted."""
        _, _, _, _, port = env  # constructed with no limits
        await port.write("/big.txt", b"x" * 10_000)
        assert await port.read("/big.txt") == b"x" * 10_000
