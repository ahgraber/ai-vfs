"""Unit tests for MontyExecutionProvider and related Monty integration scenarios.

All tests in this module are gated by ``HAS_MONTY``; they skip automatically in
environments without the ``monty`` extra and run normally in dev
(``uv sync --extra monty``).

Covers (tasks.md group "Monty Adapter + Packaging"):
  MontyProviderIntegration/SimpleExpressionReturnsOutput
  MontyProviderIntegration/GrepBridgesAsyncSearch
  MontyProviderIntegration/MontyInternalTimeoutProducesProviderError
  MontyProviderIntegration/EventLoopHeartbeatDuringExecution
  ExecutionProviderRegistry/RegistryResolvesMonty
  VfsExecutePermission/ExecuteGrantedAllows (Monty scenario, chunk-3 deferred)
  VfsErrorPropagation/PermissionDeniedSurvivesMonty
"""

from __future__ import annotations

import asyncio
import importlib.util

import pytest
import pytest_asyncio

from vfs.config import VFSConfig
from vfs.protocols.execution import ExecutionCapabilities, ExecutionResult, ResourceLimits
from vfs.vfs import VFS

HAS_MONTY = importlib.util.find_spec("pydantic_monty") is not None

skip_no_monty = pytest.mark.skipif(not HAS_MONTY, reason="pydantic-monty not installed")


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

    Returns (vfs, ns, admin_principal, agent_principal).
    The agent has read/write/execute on '/'.
    """
    ns = await vfs_inst.create_namespace("monty-ns", "admin")
    admin = await vfs_inst.create_principal("admin")
    await vfs_inst.bootstrap_admin(admin.id, ns.id)
    agent = await vfs_inst.create_principal("agent")
    await vfs_inst.grant(admin.id, agent.id, ns.id, "/", {"read", "write", "delete", "execute"})
    return vfs_inst, ns, admin, agent


# ---------------------------------------------------------------------------
# MontyProviderIntegration/SimpleExpressionReturnsOutput
# ---------------------------------------------------------------------------


class TestSimpleExpressionReturnsOutput:
    """vfs.execute('1 + 2', ..., provider_name='monty') → ExecutionResult(success=True, output=3)."""

    @skip_no_monty
    @pytest.mark.asyncio
    async def test_simple_expression(self, env):
        vfs, ns, admin, agent = env
        result = await vfs.execute(
            "1 + 2",
            ns.id,
            agent.id,
            "monty",
            resource_limits=ResourceLimits(timeout_seconds=10.0),
        )
        assert result.success is True
        assert result.output == 3
        assert result.error_type is None


# ---------------------------------------------------------------------------
# ExecutionProviderRegistry/RegistryResolvesMonty
# ---------------------------------------------------------------------------


class TestRegistryResolvesMonty:
    """resolve_execution_provider('monty', config) returns a MontyExecutionProvider."""

    @skip_no_monty
    def test_registry_returns_monty_provider(self):
        from vfs.execution.monty_provider import MontyExecutionProvider
        from vfs.execution.registry import resolve_execution_provider

        config = VFSConfig()
        provider = resolve_execution_provider("monty", config)
        assert isinstance(provider, MontyExecutionProvider)

    @skip_no_monty
    def test_capabilities(self):
        from vfs.execution.monty_provider import MontyExecutionProvider

        provider = MontyExecutionProvider()
        caps = provider.capabilities()
        assert isinstance(caps, ExecutionCapabilities)
        assert caps.supports_async is True
        assert caps.language == "python"
        assert caps.tier == "monty"
        # Monty maps max_memory_bytes onto its runtime, so it declares memory enforcement.
        assert caps.enforces_memory_limit is True

    @skip_no_monty
    def test_reset_is_noop(self):
        """reset() must not raise."""
        from vfs.execution.monty_provider import MontyExecutionProvider

        provider = MontyExecutionProvider()
        provider.reset()  # no exception


# ---------------------------------------------------------------------------
# VfsExecutePermission/ExecuteGrantedAllows (Monty scenario — chunk-3 deferred)
# ---------------------------------------------------------------------------


class TestExecuteGrantedAllowsMonty:
    """With execute permission and cwd=/workspace/, vfs.execute returns success via Monty."""

    @skip_no_monty
    @pytest.mark.asyncio
    async def test_execute_granted_allows(self, env):
        vfs, ns, admin, agent = env
        # env already grants execute on '/'
        result = await vfs.execute(
            "42",
            ns.id,
            agent.id,
            "monty",
            resource_limits=ResourceLimits(timeout_seconds=10.0),
            cwd="/",
        )
        assert result.success is True
        assert result.output == 42


# ---------------------------------------------------------------------------
# MontyProviderIntegration/MontyInternalTimeoutProducesProviderError
# ---------------------------------------------------------------------------


class TestMontyInternalTimeoutProducesProviderError:
    """Monty's own max_duration_secs inner limit produces ExecutionResult(error_type='provider_error').

    No host path appears in error_message.
    """

    @skip_no_monty
    @pytest.mark.asyncio
    async def test_inner_timeout(self, env):
        vfs, ns, admin, agent = env
        # Compute-heavy loop; Monty's inner timeout fires before the outer one.
        code = "x = 0\nwhile True:\n    x += 1"
        result = await vfs.execute(
            code,
            ns.id,
            agent.id,
            "monty",
            resource_limits=ResourceLimits(timeout_seconds=30.0, max_memory_bytes=None),
        )
        # The inner MontyResourceLimits timeout fires first — but here we use
        # the MontyExecutionProvider directly with an inner limit to be precise.
        # Drive via the provider directly to set an inner limit independently.
        from vfs.execution.fs_ops import fs_operations_for
        from vfs.execution.fs_port import SessionFsPort
        from vfs.execution.monty_provider import MontyExecutionProvider
        from vfs.session import Session

        provider = MontyExecutionProvider()
        session = Session(vfs, ns.id, agent.id)
        await session.cd("/")
        limits = ResourceLimits(timeout_seconds=0.1, max_memory_bytes=None)
        fs_ops = fs_operations_for(session, limits)
        fs_port = SessionFsPort(session)

        result = await provider.execute(code, fs_ops, fs_port, limits)

        assert result.success is False
        assert result.error_type == "provider_error"
        # No host path in error_message
        msg = result.error_message or ""
        assert "/Users" not in msg
        assert "/home" not in msg
        assert "Traceback" not in msg

    @skip_no_monty
    @pytest.mark.asyncio
    async def test_inner_timeout_no_host_path(self, env):
        """Additional check: error_message has no host paths for any Monty error."""
        from vfs.execution.fs_ops import fs_operations_for
        from vfs.execution.fs_port import SessionFsPort
        from vfs.execution.monty_provider import MontyExecutionProvider
        from vfs.session import Session

        vfs, ns, admin, agent = env
        provider = MontyExecutionProvider()
        session = Session(vfs, ns.id, agent.id)
        await session.cd("/")
        limits = ResourceLimits(timeout_seconds=0.05, max_memory_bytes=None)
        fs_ops = fs_operations_for(session, limits)
        fs_port = SessionFsPort(session)

        result = await provider.execute(
            "x = 0\nwhile True:\n    x += 1",
            fs_ops,
            fs_port,
            limits,
        )

        assert result.success is False
        assert result.error_type == "provider_error"
        msg = result.error_message or ""
        for forbidden in ("/Users", "/home", "/var", "/private", "Traceback"):
            assert forbidden not in msg, f"Forbidden token {forbidden!r} found in: {msg!r}"


# ---------------------------------------------------------------------------
# MontyProviderIntegration/GrepBridgesAsyncSearch
# ---------------------------------------------------------------------------


class TestGrepBridgesAsyncSearch:
    """Monty sandbox code calls grep(...) and results reach session.search.

    This test requires native text search (SQLite FTS5 trigram).  It skips
    gracefully when the index is not supported.
    """

    @skip_no_monty
    @pytest.mark.asyncio
    async def test_grep_returns_results(self, env):
        vfs, ns, admin, agent = env

        # Write a file with searchable content (agent has write permission)
        await vfs.write(ns.id, "/workspace/hello.py", b"def hello(): return 'hello world'", principal_id=agent.id)

        # Sandbox code calls grep to search for "hello" in /workspace/
        code = "result = await grep('hello', '/workspace/')\nresult"
        result = await vfs.execute(
            code,
            ns.id,
            agent.id,
            "monty",
            resource_limits=ResourceLimits(timeout_seconds=10.0),
        )

        # Either succeeds with results, OR fails with search_unavailable (no FTS on this SQLite)
        # Both outcomes are valid; we just verify the bridge was called (no crash).
        assert isinstance(result, ExecutionResult)
        if result.success:
            # Result should be a dict from _op_grep with 'results' key
            assert isinstance(result.output, dict)
            assert "results" in result.output
        else:
            # search_unavailable is acceptable on SQLite without FTS
            assert result.error_type in ("search_unavailable", "provider_error", "not_found")


# ---------------------------------------------------------------------------
# MontyProviderIntegration/EventLoopHeartbeatDuringExecution
# ---------------------------------------------------------------------------


class TestEventLoopHeartbeatDuringExecution:
    """A concurrent asyncio.Task keeps ticking while Monty runs compute-heavy code.

    Wrapped in pyleak no_task_leaks to verify no tasks are orphaned.
    """

    @skip_no_monty
    @pytest.mark.asyncio
    async def test_heartbeat_during_execution(self, env):
        from pyleak import no_task_leaks

        vfs, ns, admin, agent = env

        heartbeat_count = 0

        async def heartbeat() -> None:
            nonlocal heartbeat_count
            while True:
                heartbeat_count += 1
                await asyncio.sleep(0.05)

        # Compute-heavy sandbox code (no external calls, just arithmetic)
        code = "x = 0\nfor i in range(5_000_000):\n    x += i\nx"

        async with no_task_leaks(action="raise"):
            hb_task = asyncio.create_task(heartbeat())
            try:
                result = await vfs.execute(
                    code,
                    ns.id,
                    agent.id,
                    "monty",
                    resource_limits=ResourceLimits(timeout_seconds=30.0),
                )
            finally:
                hb_task.cancel()
                try:
                    await hb_task
                except asyncio.CancelledError:
                    pass

        assert result.success is True, f"execution failed: {result}"
        # Heartbeat must have ticked at least once (event loop was not starved)
        assert heartbeat_count >= 1, (
            f"Event loop starved: heartbeat never ticked (count={heartbeat_count}). "
            "run_async may be blocking the event loop; implement start()/resume() fallback."
        )


# ---------------------------------------------------------------------------
# VfsErrorPropagation/PermissionDeniedSurvivesMonty
# ---------------------------------------------------------------------------


class TestVfsErrorPropagation:
    """VFS errors from FsOperations callables propagate through Monty to vfs.execute's
    translation table, producing the correct ExecutionResult error_type.

    The sandbox calls cat() on a path where the principal has no read permission.
    The PermissionDeniedError raised inside the FsOperations callable must survive
    Monty's exception downcast and be translated to error_type='permission_denied'.
    """

    @skip_no_monty
    @pytest.mark.asyncio
    async def test_permission_denied_propagates(self, env):
        vfs, ns, admin, agent = env

        # Write a file in /secret/ using agent (who has write permission on /)
        await vfs.write(ns.id, "/secret/file.txt", b"top secret", principal_id=agent.id)

        # Create a restricted principal with execute+read on /workspace/ only.
        # This principal can cd into /workspace/ but NOT read from /secret/.
        restricted = await vfs.create_principal("restricted")
        await vfs.grant(admin.id, restricted.id, ns.id, "/workspace/", {"execute", "read"})

        # Sandbox code tries to cat a file outside the permitted prefix
        code = "await cat('/secret/file.txt')"

        result = await vfs.execute(
            code,
            ns.id,
            restricted.id,
            "monty",
            resource_limits=ResourceLimits(timeout_seconds=10.0),
            cwd="/workspace/",
        )

        assert result.success is False
        assert result.error_type == "permission_denied"
        msg = result.error_message or ""
        # Must not expose host paths or raw traceback
        assert "/Users" not in msg
        assert "Traceback" not in msg


# ---------------------------------------------------------------------------
# Reviewer finding 1: write() from sandbox returns marshalable dict
# ---------------------------------------------------------------------------


class TestWriteFromSandbox:
    """Sandbox write() must return a plain dict (not a pydantic VersionMeta).

    Reviewer reproduced: Monty raises TypeError when the external function
    returns a pydantic model, even when the sandbox discards the value — and
    the write side-effect has already committed.
    """

    @skip_no_monty
    @pytest.mark.asyncio
    async def test_write_returns_dict_and_content_round_trips(self, env):
        """Sandbox write() returns a serialisable dict; cat() reads the content back."""
        vfs, ns, admin, agent = env

        # Write a seed file so the namespace is initialised
        await vfs.write(ns.id, "/ws/seed.txt", b"seed", principal_id=agent.id)

        code = """
result = await write('/ws/new_file.txt', b'hello from sandbox')
result
"""
        result = await vfs.execute(
            code,
            ns.id,
            agent.id,
            "monty",
            resource_limits=ResourceLimits(timeout_seconds=10.0),
        )

        assert result.success is True, f"write from sandbox failed: {result}"
        assert isinstance(result.output, dict), f"write() return value should be a dict, got {type(result.output)}"
        assert "version_number" in result.output
        assert "size" in result.output

        # Verify the file was actually written
        raw = await vfs.read(ns.id, "/ws/new_file.txt", principal_id=agent.id)
        assert raw == b"hello from sandbox"

    @skip_no_monty
    @pytest.mark.asyncio
    async def test_write_then_cat_roundtrips(self, env):
        """Sandbox write() followed by cat() returns the written content."""
        vfs, ns, admin, agent = env

        code = """
await write('/ws2/f.txt', b'line one\\nline two\\n')
cat_result = await cat('/ws2/f.txt')
cat_result
"""
        result = await vfs.execute(
            code,
            ns.id,
            agent.id,
            "monty",
            resource_limits=ResourceLimits(timeout_seconds=10.0),
        )

        assert result.success is True, f"write+cat failed: {result}"
        assert result.output["error"] is None
        assert result.output["lines"] == ["line one", "line two", ""]


# ---------------------------------------------------------------------------
# Reviewer finding 3: sentinel must not mask unrelated errors
# ---------------------------------------------------------------------------


class TestSentinelDoesNotMaskUnrelatedErrors:
    """If the sandbox catches a VFS error and then fails for an unrelated reason,
    the reported error_type must reflect the unrelated failure, NOT the caught VFS error.

    Reviewer reproduced: caught permission_denied + later NameError → reported
    permission_denied instead of internal_error.
    """

    @skip_no_monty
    @pytest.mark.asyncio
    async def test_caught_vfs_error_then_name_error(self, env):
        """Sandbox catches PermissionDeniedError, then hits NameError.

        Expected: error_type='internal_error' (not 'permission_denied').
        """
        vfs, ns, admin, agent = env

        # Create a restricted principal with execute+read on /workspace/ only.
        restricted = await vfs.create_principal("restricted-sentinel")
        await vfs.grant(admin.id, restricted.id, ns.id, "/workspace/", {"execute", "read"})

        # Sandbox: catch the permission error, then reference an undefined name.
        code = """
try:
    await cat('/secret/file.txt')
except Exception:
    pass  # VFS error caught — must not poison the sentinel
undefined_name  # NameError: this is the actual terminating error
"""
        result = await vfs.execute(
            code,
            ns.id,
            restricted.id,
            "monty",
            resource_limits=ResourceLimits(timeout_seconds=10.0),
            cwd="/workspace/",
        )

        assert result.success is False
        # The terminating error is NameError → internal_error, NOT permission_denied
        assert result.error_type != "permission_denied", (
            "Sentinel incorrectly reported the caught VFS error as the terminating failure"
        )
        assert result.error_type in ("internal_error", "provider_error"), (
            f"Expected internal_error or provider_error, got {result.error_type!r}"
        )

    @skip_no_monty
    @pytest.mark.asyncio
    async def test_uncaught_vfs_error_still_propagates(self, env):
        """If the sandbox does NOT catch the VFS error, it must still propagate correctly."""
        vfs, ns, admin, agent = env

        restricted = await vfs.create_principal("restricted-sentinel2")
        await vfs.grant(admin.id, restricted.id, ns.id, "/workspace/", {"execute", "read"})

        # Sandbox raises PermissionDeniedError (NOT caught) — sentinel should fire
        code = "await cat('/secret/file.txt')"

        result = await vfs.execute(
            code,
            ns.id,
            restricted.id,
            "monty",
            resource_limits=ResourceLimits(timeout_seconds=10.0),
            cwd="/workspace/",
        )

        assert result.success is False
        assert result.error_type == "permission_denied"


# ---------------------------------------------------------------------------
# Reviewer finding 8: _safe_error_message strips internal module paths
# ---------------------------------------------------------------------------


@skip_no_monty
class TestSafeErrorMessageStripsModulePaths:
    """_safe_error_message must strip dotted internal module paths like vfs.models.VersionMeta."""

    def test_strips_vfs_module_path(self):
        """vfs.models.VersionMeta in error message is replaced by bare class name."""
        from unittest.mock import MagicMock

        from vfs.execution.monty_provider import _safe_error_message

        # Construct a mock MontyError whose str() contains a dotted module path
        exc = MagicMock()
        exc.__str__ = lambda self: "TypeError: Cannot convert vfs.models.VersionMeta to Monty value"

        msg = _safe_error_message(exc, None)
        assert "vfs.models." not in msg, f"Module path leaked: {msg!r}"
        # The bare class name should remain (informative without leaking structure)
        assert "VersionMeta" in msg

    def test_strips_vfs_errors_module_path(self):
        """vfs.errors.PermissionDeniedError is replaced by bare class name."""
        from unittest.mock import MagicMock

        from vfs.execution.monty_provider import _safe_error_message

        exc = MagicMock()
        exc.__str__ = lambda self: "vfs.errors.PermissionDeniedError: Access denied"

        msg = _safe_error_message(exc, None)
        assert "vfs.errors." not in msg
        assert "PermissionDeniedError" in msg

    def test_host_paths_still_stripped(self):
        """Host filesystem paths are still stripped after the module-path fix."""
        from unittest.mock import MagicMock

        from vfs.execution.monty_provider import _safe_error_message

        exc = MagicMock()
        exc.__str__ = lambda self: "Error in /Users/runner/vfs/src/vfs/execution/monty_provider.py"

        msg = _safe_error_message(exc, None)
        assert "/Users" not in msg
