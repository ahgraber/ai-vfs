"""Unit tests for JustBashExecutionProvider — bash over the governed VFS.

All tests are gated by ``HAS_JUST_BASH``; they skip automatically where the
``just-bash`` extra is absent and run normally in dev (``uv sync --extra just-bash``).

Covers the ``JustBashProvider`` requirement scenarios (tasks.md group
"execution — just-bash provider"):
  JustBashProvider/BashCatReadsVfsFile
  JustBashProvider/BashWritePersistsVersion
  JustBashProvider/GrepRoutesToSearchIndex
  JustBashProvider/BashRespectsPermissions

The act phases are wrapped in ``pyleak.no_task_leaks`` because ``Bash.exec`` runs
the bash program asynchronously over the async FS-port adapter.
"""

from __future__ import annotations

import importlib.util

from pyleak import no_task_leaks
import pytest
import pytest_asyncio

from vfs.config import VFSConfig
from vfs.errors import VFSError
from vfs.protocols.execution import ExecutionCapabilities, ExecutionResult, ResourceLimits
from vfs.vfs import VFS

HAS_JUST_BASH = importlib.util.find_spec("just_bash") is not None

skip_no_just_bash = pytest.mark.skipif(not HAS_JUST_BASH, reason="just-bash not installed")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def vfs_inst(tmp_path):
    """Lightweight in-process VFS backed by SQLite + local FS blob."""
    db_path = str(tmp_path / "test.db")
    blob_path = str(tmp_path / "blobs")
    config = VFSConfig(
        metadata_store_uri=f"sqlite:///{db_path}",
        blob_store_uri=f"file:///{blob_path}/",
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
    """Bootstrap namespace + admin + agent principals.

    Returns (vfs, ns, admin_principal, agent_principal); the agent has
    read/write/delete/execute on '/'.
    """
    ns = await vfs_inst.create_namespace("jb-ns", "admin")
    admin = await vfs_inst.create_principal("admin")
    await vfs_inst.bootstrap_admin(admin.id, ns.id)
    agent = await vfs_inst.create_principal("agent")
    await vfs_inst.grant(admin.id, agent.id, ns.id, "/", {"read", "write", "delete", "execute"})
    return vfs_inst, ns, admin, agent


# ---------------------------------------------------------------------------
# JustBashProvider/BashCatReadsVfsFile
# ---------------------------------------------------------------------------


class TestBashCatReadsVfsFile:
    """Sandboxed bash ``cat`` reads the VFS file's current content from stdout."""

    @skip_no_just_bash
    @pytest.mark.asyncio
    async def test_cat_reads_vfs_file(self, env):
        vfs, ns, admin, agent = env
        await vfs.write(ns.id, "/work/a.txt", b"hello\nworld\n", principal_id=agent.id)

        async with no_task_leaks(action="raise"):
            result = await vfs.execute(
                "cat /work/a.txt",
                ns.id,
                agent.id,
                "just-bash",
                resource_limits=ResourceLimits(timeout_seconds=10.0),
                cwd="/",
            )

        assert result.success is True, f"cat failed: {result}"
        assert result.output == "hello\nworld\n"
        assert result.error_type is None


# ---------------------------------------------------------------------------
# JustBashProvider/BashWritePersistsVersion
# ---------------------------------------------------------------------------


class TestBashWritePersistsVersion:
    """Sandboxed bash redirection (``> /path``) creates a new VFS version."""

    @skip_no_just_bash
    @pytest.mark.asyncio
    async def test_redirection_persists_version(self, env):
        vfs, ns, admin, agent = env

        async with no_task_leaks(action="raise"):
            result = await vfs.execute(
                "echo written > /work/b.txt",
                ns.id,
                agent.id,
                "just-bash",
                resource_limits=ResourceLimits(timeout_seconds=10.0),
                cwd="/",
            )

        assert result.success is True, f"redirection write failed: {result}"
        # The written content is readable as a new VFS version.
        content = await vfs.read(ns.id, "/work/b.txt", principal_id=agent.id)
        assert content == b"written\n"
        meta = await vfs.stat(ns.id, "/work/b.txt", principal_id=agent.id)
        assert meta.current_version_number >= 1


# ---------------------------------------------------------------------------
# JustBashProvider/GrepRoutesToSearchIndex
# ---------------------------------------------------------------------------


class TestGrepRoutesToSearchIndex:
    """The overridden ``grep`` resolves to the VFS search index, not brute-force.

    Driven through the provider directly so the session-bound ``FsOperations.grep``
    (the index-routed wrapper) can be spied: a brute-force bash ``grep`` over the
    ``fs=`` adapter would never touch it.
    """

    @skip_no_just_bash
    @pytest.mark.asyncio
    async def test_grep_routes_to_index(self, env):
        from vfs.execution.fs_ops import fs_operations_for
        from vfs.execution.fs_port import SessionFsPort
        from vfs.execution.just_bash_provider import JustBashExecutionProvider
        from vfs.session import Session

        vfs, ns, admin, agent = env
        await vfs.write(ns.id, "/work/hello.py", b"def hello():\n    return 'hello world'\n", principal_id=agent.id)

        provider = JustBashExecutionProvider()
        session = Session(vfs, ns.id, agent.id)
        await session.cd("/")
        limits = ResourceLimits(timeout_seconds=10.0)
        fs_ops = fs_operations_for(session, limits)
        fs_port = SessionFsPort(session)

        # Spy on the index-routed grep wrapper to prove the override invoked it.
        grep_calls: list[tuple] = []
        original_grep = fs_ops.grep

        async def spy_grep(*args, **kwargs):
            grep_calls.append((args, kwargs))
            return await original_grep(*args, **kwargs)

        fs_ops.grep = spy_grep

        async with no_task_leaks(action="raise"):
            try:
                result = await provider.execute("grep hello /work", fs_ops, fs_port, limits)
            except VFSError:
                # A search backend without native text index (no SQLite FTS5) raises;
                # routing to the index is still proven by the spy below.
                result = None

        assert grep_calls, "grep did not route to the VFS search index (override not invoked)"
        assert grep_calls[0][0][0] == "hello"
        if result is not None:
            assert result.success is True, f"grep failed: {result}"
            assert "/work/hello.py" in result.output


# ---------------------------------------------------------------------------
# JustBashProvider/BashRespectsPermissions
# ---------------------------------------------------------------------------


class TestBashRespectsPermissions:
    """Sandboxed bash ``cat`` on an unauthorized path is denied, not bypassed."""

    @skip_no_just_bash
    @pytest.mark.asyncio
    async def test_unauthorized_cat_denied(self, env):
        vfs, ns, admin, agent = env
        # Seed a secret file (agent can write under '/').
        await vfs.write(ns.id, "/secret/file.txt", b"top secret", principal_id=agent.id)

        # A principal with execute+read on '/pub/' only — cannot read '/secret/'.
        restricted = await vfs.create_principal("restricted")
        await vfs.grant(admin.id, restricted.id, ns.id, "/pub/", {"execute", "read"})

        async with no_task_leaks(action="raise"):
            result = await vfs.execute(
                "cat /secret/file.txt",
                ns.id,
                restricted.id,
                "just-bash",
                resource_limits=ResourceLimits(timeout_seconds=10.0),
                cwd="/pub/",
            )

        assert result.success is False
        assert result.error_type == "permission_denied"
        msg = result.error_message or ""
        # No host path or raw traceback leaks into the sandbox-visible message.
        assert "/Users" not in msg
        assert "Traceback" not in msg
        # The secret content is never produced.
        assert "top secret" not in (result.output or "")


# ---------------------------------------------------------------------------
# JustBashProvider capabilities / registry resolution
# ---------------------------------------------------------------------------


class TestProviderCapabilities:
    """capabilities() declares the bash tier; reset() is a no-op; the registry resolves it."""

    @skip_no_just_bash
    def test_capabilities(self):
        from vfs.execution.just_bash_provider import JustBashExecutionProvider

        caps = JustBashExecutionProvider().capabilities()
        assert isinstance(caps, ExecutionCapabilities)
        assert caps.supports_async is True
        assert caps.language == "bash"
        assert caps.tier == "just-bash"

    @skip_no_just_bash
    def test_reset_is_noop(self):
        from vfs.execution.just_bash_provider import JustBashExecutionProvider

        JustBashExecutionProvider().reset()  # no exception

    @skip_no_just_bash
    def test_registry_resolves_just_bash(self):
        from vfs.execution.just_bash_provider import JustBashExecutionProvider
        from vfs.execution.registry import resolve_execution_provider

        provider = resolve_execution_provider("just-bash", VFSConfig())
        assert isinstance(provider, JustBashExecutionProvider)
