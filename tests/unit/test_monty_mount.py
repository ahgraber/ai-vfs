"""Unit tests for the Monty native filesystem mount (MontyVfsOS / FS-port).

All tests are gated by ``HAS_MONTY``.

Covers `MontyNativeFilesystemMount`:
  NativeOpenReadsVfsFile
  NativeWritePersistsVersion
  MountEnforcesPermissions
  NativeMountDenialTranslatesToPermissionDenied
  MountDoesNotExposeHostFilesystem
  HostEventLoopNotBlockedDuringNativeFsCalls
and `MontyProviderIntegration/NativeFilesystemAccessFromSandbox`.
"""

from __future__ import annotations

import asyncio
import importlib.util

import pytest
import pytest_asyncio

from vfs.config import VFSConfig
from vfs.protocols.execution import ResourceLimits
from vfs.vfs import VFS

HAS_MONTY = importlib.util.find_spec("pydantic_monty") is not None
skip_no_monty = pytest.mark.skipif(not HAS_MONTY, reason="pydantic-monty not installed")


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
    """Returns (vfs, ns, admin, agent); agent has full rights on '/'."""
    ns = await vfs_inst.create_namespace("mount-ns", "admin")
    admin = await vfs_inst.create_principal("admin")
    await vfs_inst.bootstrap_admin(admin.id, ns.id)
    agent = await vfs_inst.create_principal("agent")
    await vfs_inst.grant(admin.id, agent.id, ns.id, "/", {"read", "write", "delete", "execute"})
    return vfs_inst, ns, admin, agent


class TestNativeFilesystemMount:
    @skip_no_monty
    @pytest.mark.asyncio
    async def test_native_pathlib_read(self, env):
        vfs, ns, _, agent = env
        await vfs.write(ns.id, "/ws/a.txt", b"hello\nworld", principal_id=agent.id)
        code = "from pathlib import Path\nPath('/ws/a.txt').read_text()"
        result = await vfs.execute(code, ns.id, agent.id, "monty", cwd="/ws/")
        assert result.success is True
        assert result.output == "hello\nworld"

    @skip_no_monty
    @pytest.mark.asyncio
    async def test_native_open_read(self, env):
        vfs, ns, _, agent = env
        await vfs.write(ns.id, "/ws/a.txt", b"opened", principal_id=agent.id)
        code = "open('/ws/a.txt').read()"
        result = await vfs.execute(code, ns.id, agent.id, "monty", cwd="/ws/")
        assert result.success is True
        assert result.output == "opened"

    @skip_no_monty
    @pytest.mark.asyncio
    async def test_native_write_persists_version(self, env):
        vfs, ns, _, agent = env
        code = "from pathlib import Path\nPath('/ws/new.txt').write_text('native write')\n'done'"
        result = await vfs.execute(code, ns.id, agent.id, "monty", cwd="/ws/")
        assert result.success is True
        content = await vfs.read(ns.id, "/ws/new.txt", principal_id=agent.id)
        assert content == b"native write"

    @skip_no_monty
    @pytest.mark.asyncio
    async def test_native_access_without_injected_verb(self, env):
        """MontyProviderIntegration/NativeFilesystemAccessFromSandbox."""
        vfs, ns, _, agent = env
        await vfs.write(ns.id, "/ws/data.txt", b"42", principal_id=agent.id)
        # Uses pathlib only — no cat/grep/edit injected verb.
        code = "from pathlib import Path\nint(Path('/ws/data.txt').read_text())"
        result = await vfs.execute(code, ns.id, agent.id, "monty", cwd="/ws/")
        assert result.success is True
        assert result.output == 42

    @skip_no_monty
    @pytest.mark.asyncio
    async def test_mount_denial_translates_to_permission_denied(self, vfs_inst):
        """An unauthorized native read surfaces error_type='permission_denied'."""
        ns = await vfs_inst.create_namespace("ns2", "admin")
        admin = await vfs_inst.create_principal("admin2")
        await vfs_inst.bootstrap_admin(admin.id, ns.id)
        # Author a secret outside the agent's grant.
        author = await vfs_inst.create_principal("author")
        await vfs_inst.grant(admin.id, author.id, ns.id, "/", {"read", "write"})
        await vfs_inst.write(ns.id, "/secret/data.txt", b"top secret", principal_id=author.id)
        # Agent may execute + read only under /ws/.
        agent = await vfs_inst.create_principal("agent")
        await vfs_inst.grant(admin.id, agent.id, ns.id, "/ws/", {"read", "execute"})
        code = "from pathlib import Path\nPath('/secret/data.txt').read_text()"
        result = await vfs_inst.execute(code, ns.id, agent.id, "monty", cwd="/ws/")
        assert result.success is False
        assert result.error_type == "permission_denied"
        assert "/Users" not in (result.error_message or "")

    @skip_no_monty
    @pytest.mark.asyncio
    async def test_host_filesystem_not_reachable(self, env):
        vfs, ns, _, agent = env
        code = "from pathlib import Path\nPath('/etc/hosts').read_text()"
        result = await vfs.execute(code, ns.id, agent.id, "monty", cwd="/")
        # Host file is not reachable: the mount routes to the VFS, which has no such file.
        assert result.success is False
        assert result.error_type == "not_found"

    @skip_no_monty
    @pytest.mark.asyncio
    async def test_host_event_loop_not_blocked(self, env):
        """A concurrent heartbeat task keeps ticking during native FS calls."""
        from pyleak import no_thread_leaks

        vfs, ns, _, agent = env
        await vfs.write(ns.id, "/ws/a.txt", b"x" * 100, principal_id=agent.id)

        ticks = 0
        stop = False

        async def heartbeat():
            nonlocal ticks
            while not stop:
                ticks += 1
                await asyncio.sleep(0.005)

        code = "\n".join(
            ["from pathlib import Path", "for _ in range(20):", "    Path('/ws/a.txt').read_text()", "'ok'"]
        )
        async with no_thread_leaks(action="raise"):
            hb = asyncio.create_task(heartbeat())
            result = await vfs.execute(code, ns.id, agent.id, "monty", cwd="/ws/")
            stop = True
            await hb

        assert result.success is True
        assert ticks > 0, "event loop heartbeat did not tick during native FS calls"
