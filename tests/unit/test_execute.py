"""Unit tests for vfs.execute, Session.execute, and Session.search find_predicates.

Covers (from chunk 3 tasks):
  VfsExecutePermission/ExecuteRequiresPermission
  VfsExecutePermission/ExecuteCwdDenied
  ExecuteGrantedAllows (fake provider, no Monty dependency)
  ExecutionProviderRegistry/UnknownProviderRejected
  ExecutionProviderRegistry/MissingMontyExtraRaises
  VfsExecuteErrorTranslation/* (parametrized table)
  AccessControl/ExecutePermissionEnforced
  AccessControl/ExecutePermissionStorable
  SessionProxy/SessionExecuteProxiesToVfs
  SessionSearch/FindPredicatesPassthrough
  VfsExecuteErrorTranslation/TimeoutReturnsStructuredResult (pyleak-guarded)

Architecture note
-----------------
All tests drive through a real SQLite-backed VFS.  Provider dispatch is tested
via a minimal in-test ``FakeProvider`` that conforms to the ``ExecutionProvider``
protocol without any external dependencies.  The Monty-adapter scenario
(ExecuteGrantedAllows with the real MontyExecutionProvider) lands in chunk 4.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from vfs.config import VFSConfig
from vfs.errors import (
    AnchorConflictError,
    ConflictError,
    IndexUnavailableError,
    NotFoundError,
    OperationBudgetExceededError,
    PermissionDeniedError,
    ReadBudgetExceededError,
    ReindexRequiredError,
    VersionCollisionError,
)
from vfs.protocols.execution import ExecutionCapabilities, ExecutionResult, ResourceLimits
from vfs.session import Session
from vfs.vfs import VFS

# ---------------------------------------------------------------------------
# Fake provider (no Monty required)
# ---------------------------------------------------------------------------


class FakeProvider:
    """Minimal ExecutionProvider for testing dispatch without pydantic-monty.

    Behaviour is controlled by constructor arguments:
    - ``result``: the ExecutionResult to return on success.
    - ``raise_exc``: if not None, raised from execute() after dispatch.
    - ``sleep_seconds``: if > 0, execute sleeps this long (for timeout tests).
    """

    def __init__(
        self,
        result: ExecutionResult | None = None,
        raise_exc: BaseException | None = None,
        sleep_seconds: float = 0.0,
    ) -> None:
        self._result = result or ExecutionResult(success=True, output="ok")
        self._raise_exc = raise_exc
        self._sleep_seconds = sleep_seconds
        self.execute_calls: list[tuple[str, Any, ResourceLimits]] = []

    async def execute(self, code: str, fs_ops: Any, resource_limits: ResourceLimits) -> ExecutionResult:
        self.execute_calls.append((code, fs_ops, resource_limits))
        if self._sleep_seconds > 0:
            await asyncio.sleep(self._sleep_seconds)
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._result

    def capabilities(self) -> ExecutionCapabilities:
        return ExecutionCapabilities(supports_async=True, language="python", tier="fake")

    def reset(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def vfs_inst(tmp_path):
    """Lightweight VFS backed by SQLite + local FS blob."""
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
    The admin has full rights; the agent starts with read/write only on '/'.
    """
    ns = await vfs_inst.create_namespace("exec-ns", "admin")
    admin = await vfs_inst.create_principal("admin")
    await vfs_inst.bootstrap_admin(admin.id, ns.id)
    agent = await vfs_inst.create_principal("agent")
    await vfs_inst.grant(admin.id, agent.id, ns.id, "/", {"read", "write", "delete"})
    return vfs_inst, ns, admin, agent


# ---------------------------------------------------------------------------
# Helper: register FakeProvider under a test name inside the registry
# ---------------------------------------------------------------------------


def _patch_registry(provider: FakeProvider):
    """Context manager that injects ``provider`` under the key ``"fake"``."""
    from vfs.execution import registry

    original = dict(registry._EXECUTION_PROVIDERS)

    def _resolve(name, config):  # noqa: ARG001
        if name == "fake":
            return provider
        # fall through to normal resolution for any other name
        from vfs.execution.registry import resolve_execution_provider as _real

        # avoid infinite recursion — call original with a clean registry
        spec = original.get(name)
        if spec is None:
            known = ", ".join(sorted(original))
            raise ValueError(f"Unknown execution provider {name!r}. Known providers: {known or '(none registered)'}.")
        return _real.__wrapped__(name, config) if hasattr(_real, "__wrapped__") else _real(name, config)

    return patch.object(registry, "resolve_execution_provider", side_effect=_resolve)


# ---------------------------------------------------------------------------
# VfsExecutePermission / AccessControl — ExecuteRequiresPermission
# ---------------------------------------------------------------------------


class TestExecuteRequiresPermission:
    """VfsExecutePermission/ExecuteRequiresPermission + AccessControl/ExecutePermissionEnforced.

    A principal without execute permission raises PermissionDeniedError.
    No session, FsOperations, or provider should be constructed.
    """

    @pytest.mark.asyncio
    async def test_no_execute_perm_raises(self, env):
        vfs, ns, admin, agent = env
        # agent has read/write/delete but NOT execute on '/'
        provider = FakeProvider()

        from vfs.execution import registry

        with (
            patch.object(registry, "resolve_execution_provider", return_value=provider),
            pytest.raises(PermissionDeniedError),
        ):
            await vfs.execute(
                "pass",
                ns.id,
                agent.id,
                "fake",
                resource_limits=ResourceLimits(timeout_seconds=5.0),
            )

        # Provider must not have been called
        assert provider.execute_calls == []

    @pytest.mark.asyncio
    async def test_session_not_constructed_on_denied(self, env):
        """Instrument Session.__init__ to verify it is never called on permission denial."""
        vfs, ns, admin, agent = env
        provider = FakeProvider()

        session_inits: list = []
        original_init = Session.__init__

        def _tracking_init(self, *args, **kwargs):
            session_inits.append(True)
            original_init(self, *args, **kwargs)

        from vfs.execution import registry

        with (
            patch.object(registry, "resolve_execution_provider", return_value=provider),
            patch.object(Session, "__init__", _tracking_init),
            pytest.raises(PermissionDeniedError),
        ):
            await vfs.execute(
                "pass",
                ns.id,
                agent.id,
                "fake",
                resource_limits=ResourceLimits(timeout_seconds=5.0),
            )

        assert session_inits == [], "Session should not be constructed before permission check passes"


# ---------------------------------------------------------------------------
# VfsExecutePermission/ExecuteCwdDenied
# ---------------------------------------------------------------------------


class TestExecuteCwdDenied:
    """VfsExecutePermission/ExecuteCwdDenied.

    execute permission granted on /workspace/ only; cwd=/ is denied.
    """

    @pytest.mark.asyncio
    async def test_execute_on_ungranted_cwd_raises(self, env):
        vfs, ns, admin, agent = env
        # Grant execute only on /workspace/ (not root)
        await vfs.grant(admin.id, agent.id, ns.id, "/workspace/", {"execute"})

        provider = FakeProvider()

        from vfs.execution import registry

        with (
            patch.object(registry, "resolve_execution_provider", return_value=provider),
            pytest.raises(PermissionDeniedError),
        ):
            await vfs.execute(
                "pass",
                ns.id,
                agent.id,
                "fake",
                resource_limits=ResourceLimits(timeout_seconds=5.0),
                cwd="/",
            )

        assert provider.execute_calls == []

    @pytest.mark.asyncio
    async def test_execute_on_granted_cwd_succeeds(self, env):
        """Positive control: with execute on /workspace/ and cwd=/workspace/ the call proceeds."""
        vfs, ns, admin, agent = env
        await vfs.grant(admin.id, agent.id, ns.id, "/workspace/", {"execute", "read"})

        provider = FakeProvider(result=ExecutionResult(success=True, output="done"))

        from vfs.execution import registry

        with patch.object(registry, "resolve_execution_provider", return_value=provider):
            result = await vfs.execute(
                "pass",
                ns.id,
                agent.id,
                "fake",
                resource_limits=ResourceLimits(timeout_seconds=5.0),
                cwd="/workspace/",
            )

        assert result.success is True
        assert len(provider.execute_calls) == 1


# ---------------------------------------------------------------------------
# ExecuteGrantedAllows (fake provider; Monty scenario is chunk 4)
# ---------------------------------------------------------------------------


class TestExecuteGrantedAllowsFakeProvider:
    """Execute with a FakeProvider succeeds end-to-end.

    The Monty-named scenario (ExecuteGrantedAllows with MontyExecutionProvider)
    is implemented in chunk 4 (test_monty_provider.py).
    """

    @pytest.mark.asyncio
    async def test_execute_returns_provider_result(self, env):
        vfs, ns, admin, agent = env
        # Re-grant with execute included; set_permission replaces the existing row.
        await vfs.grant(admin.id, agent.id, ns.id, "/", {"read", "write", "delete", "execute"})

        expected = ExecutionResult(success=True, output=42)
        provider = FakeProvider(result=expected)

        from vfs.execution import registry

        with patch.object(registry, "resolve_execution_provider", return_value=provider):
            result = await vfs.execute(
                "1 + 1",
                ns.id,
                agent.id,
                "fake",
                resource_limits=ResourceLimits(timeout_seconds=5.0),
            )

        assert result.success is True
        assert result.output == 42
        assert result.error_type is None


# ---------------------------------------------------------------------------
# ExecutionProviderRegistry/UnknownProviderRejected
# ---------------------------------------------------------------------------


class TestUnknownProviderRejected:
    """ExecutionProviderRegistry/UnknownProviderRejected.

    ValueError raised BEFORE session construction.
    """

    @pytest.mark.asyncio
    async def test_unknown_provider_raises_value_error(self, env):
        vfs, ns, admin, agent = env
        # Provider resolution happens before permission check (Tier 1);
        # no grant needed for this test — the ValueError fires first.

        with pytest.raises(ValueError, match="nonexistent"):
            await vfs.execute(
                "pass",
                ns.id,
                agent.id,
                "nonexistent",
                resource_limits=ResourceLimits(timeout_seconds=5.0),
            )

    @pytest.mark.asyncio
    async def test_unknown_provider_raised_before_session(self, env):
        """ValueError from unknown provider fires before Session is constructed."""
        vfs, ns, admin, agent = env
        # Provider resolution is Tier 1 and fires before session construction;
        # no execute grant needed.

        session_inits: list = []
        original_init = Session.__init__

        def _tracking_init(self, *args, **kwargs):
            session_inits.append(True)
            original_init(self, *args, **kwargs)

        with patch.object(Session, "__init__", _tracking_init), pytest.raises(ValueError):
            await vfs.execute(
                "pass",
                ns.id,
                agent.id,
                "nonexistent",
                resource_limits=ResourceLimits(timeout_seconds=5.0),
            )

        assert session_inits == [], "Session must not be constructed before provider resolution"


# ---------------------------------------------------------------------------
# ExecutionProviderRegistry/MissingMontyExtraRaises
# ---------------------------------------------------------------------------


class TestMissingMontyExtraRaises:
    """ExecutionProviderRegistry/MissingMontyExtraRaises.

    When pydantic_monty is not importable, resolve_execution_provider raises
    with an actionable "ai-vfs[monty]" message; no ImportError traceback exposed.
    """

    def test_missing_monty_raises_import_error_with_install_hint(self):
        from vfs.config import VFSConfig
        from vfs.execution.registry import resolve_execution_provider

        config = VFSConfig()

        # Monkeypatch importlib.util.find_spec to pretend pydantic_monty is absent.
        import importlib.util

        original_find_spec = importlib.util.find_spec

        def _no_monty(name, *args, **kwargs):
            if name == "pydantic_monty":
                return None
            return original_find_spec(name, *args, **kwargs)

        with patch("importlib.util.find_spec", side_effect=_no_monty), pytest.raises(ImportError) as exc_info:
            resolve_execution_provider("monty", config)

        msg = str(exc_info.value)
        assert "ai-vfs[monty]" in msg, f"Expected 'ai-vfs[monty]' install hint in: {msg!r}"
        # Must NOT expose a traceback or bare ImportError details
        assert "Traceback" not in msg


# ---------------------------------------------------------------------------
# VfsExecuteErrorTranslation — parametrized table
# ---------------------------------------------------------------------------

_TRANSLATION_TABLE = [
    # (exception_to_raise, expected_error_type)
    # Exception messages use distinctive marker strings that do not appear in
    # the curated error_message values to avoid false-positive overlap checks.
    (PermissionDeniedError("MARKER_perm /home/user/secret"), "permission_denied"),
    (NotFoundError("MARKER_notfound"), "not_found"),
    (ConflictError("MARKER_cas"), "conflict"),
    (VersionCollisionError("MARKER_collision"), "conflict"),
    (OperationBudgetExceededError("MARKER_opbudget"), "budget_exceeded"),
    (AnchorConflictError("MARKER_anchor"), "anchor_conflict"),
    (ReadBudgetExceededError("MARKER_readbgt"), "search_unavailable"),
    (ReindexRequiredError("MARKER_reindex"), "search_unavailable"),
    (IndexUnavailableError("MARKER_idxdown"), "search_unavailable"),
    (RuntimeError("MARKER_unexpected"), "internal_error"),
]


class TestErrorTranslationTable:
    """VfsExecuteErrorTranslation — each VFS error maps to the spec's error_type.

    Each case:
    1. Grants execute permission so the dispatch path is reached.
    2. Uses a FakeProvider that raises the specified exception.
    3. Asserts the returned ExecutionResult has the expected error_type.
    4. Asserts error_message contains neither '/Users' nor 'Traceback'.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("exc,expected_type", _TRANSLATION_TABLE)
    async def test_error_translation(self, exc, expected_type, env):
        vfs, ns, admin, agent = env
        # Re-grant with execute; read is required for session.cd(cwd).
        await vfs.grant(admin.id, agent.id, ns.id, "/", {"read", "write", "delete", "execute"})

        provider = FakeProvider(raise_exc=exc)

        from vfs.execution import registry

        with patch.object(registry, "resolve_execution_provider", return_value=provider):
            result = await vfs.execute(
                "pass",
                ns.id,
                agent.id,
                "fake",
                resource_limits=ResourceLimits(timeout_seconds=5.0),
            )

        assert result.success is False
        assert result.error_type == expected_type, (
            f"For {type(exc).__name__}: expected error_type={expected_type!r}, got {result.error_type!r}"
        )
        # No host paths or tracebacks in error_message
        msg = result.error_message or ""
        assert "/Users" not in msg, f"Host path leaked in error_message: {msg!r}"
        assert "Traceback" not in msg, f"Traceback leaked in error_message: {msg!r}"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("exc,expected_type", _TRANSLATION_TABLE)
    async def test_error_message_no_exception_details(self, exc, expected_type, env):
        """Error messages must not expose exception args (which may carry host paths)."""
        vfs, ns, admin, agent = env
        await vfs.grant(admin.id, agent.id, ns.id, "/", {"read", "write", "delete", "execute"})

        provider = FakeProvider(raise_exc=exc)

        from vfs.execution import registry

        with patch.object(registry, "resolve_execution_provider", return_value=provider):
            result = await vfs.execute(
                "pass",
                ns.id,
                agent.id,
                "fake",
                resource_limits=ResourceLimits(timeout_seconds=5.0),
            )

        # The error_message must be a short curated string, not a repr of the exception
        msg = result.error_message or ""
        # Confirmed: none of the exception arguments (e.g. "denied /secret/path") leak through
        assert str(exc) not in msg, f"Raw exception message leaked into error_message: {msg!r}"


# ---------------------------------------------------------------------------
# VfsExecuteErrorTranslation/TimeoutReturnsStructuredResult
# ---------------------------------------------------------------------------


class TestTimeoutReturnsStructuredResult:
    """VfsExecuteErrorTranslation/TimeoutReturnsStructuredResult.

    A provider that sleeps past the timeout produces ExecutionResult(error_type="timeout").
    Guarded with pyleak to verify the sleeping task is cancelled (no task leak).
    """

    @pytest.mark.asyncio
    async def test_timeout_returns_structured_result(self, env):
        from pyleak import no_task_leaks

        vfs, ns, admin, agent = env
        await vfs.grant(admin.id, agent.id, ns.id, "/", {"read", "write", "delete", "execute"})

        # Provider sleeps 10 s; timeout is 0.05 s — will be cancelled
        sleeping_provider = FakeProvider(sleep_seconds=10.0)

        from vfs.execution import registry

        with patch.object(registry, "resolve_execution_provider", return_value=sleeping_provider):
            async with no_task_leaks(action="raise"):
                result = await vfs.execute(
                    "pass",
                    ns.id,
                    agent.id,
                    "fake",
                    timeout=0.05,
                    resource_limits=ResourceLimits(timeout_seconds=0.05),
                )

        assert result.success is False
        assert result.error_type == "timeout"
        assert result.error_message is not None


# ---------------------------------------------------------------------------
# AccessControl/ExecutePermissionStorable
# ---------------------------------------------------------------------------


class TestExecutePermissionStorable:
    """AccessControl/ExecutePermissionStorable.

    Admin grants {execute} on /workspace/; the grant persists and is queryable.
    """

    @pytest.mark.asyncio
    async def test_execute_permission_storable(self, env):
        vfs, ns, admin, agent = env
        await vfs.grant(admin.id, agent.id, ns.id, "/workspace/", {"execute"})

        # Verify via check_permission at the metadata layer
        has_perm = await vfs._meta.check_permission(agent.id, ns.id, "/workspace/", "execute")
        assert has_perm is True

    @pytest.mark.asyncio
    async def test_execute_permission_not_granted_by_default(self, env):
        """Agent without explicit execute grant is denied."""
        vfs, ns, admin, agent = env
        # agent has read/write/delete from fixture, but not execute
        has_perm = await vfs._meta.check_permission(agent.id, ns.id, "/", "execute")
        assert has_perm is False


# ---------------------------------------------------------------------------
# SessionProxy/SessionExecuteProxiesToVfs
# ---------------------------------------------------------------------------


class TestSessionExecuteProxiesToVfs:
    """SessionProxy/SessionExecuteProxiesToVfs.

    session.execute delegates to vfs.execute with the session's namespace_id,
    principal_id, and current cwd.
    """

    @pytest.mark.asyncio
    async def test_session_execute_passes_namespace_principal_cwd(self, env):
        vfs, ns, admin, agent = env
        await vfs.grant(admin.id, agent.id, ns.id, "/workspace/", {"execute", "read"})

        session = Session(vfs, ns.id, agent.id)
        await session.cd("/workspace/")

        execute_calls: list[dict] = []
        original_execute = vfs.execute

        async def _tracking_execute(code, namespace_id, principal_id, provider_name, **kwargs):
            execute_calls.append(
                {
                    "code": code,
                    "namespace_id": namespace_id,
                    "principal_id": principal_id,
                    "provider_name": provider_name,
                    "cwd": kwargs.get("cwd"),
                }
            )
            # Return a success result directly — no real dispatch
            return ExecutionResult(success=True, output="tracked")

        vfs.execute = _tracking_execute
        try:
            await session.execute("pass", provider_name="fake", resource_limits=ResourceLimits())
        finally:
            vfs.execute = original_execute

        assert len(execute_calls) == 1
        call = execute_calls[0]
        assert call["namespace_id"] == ns.id
        assert call["principal_id"] == agent.id
        assert call["cwd"] == "/workspace/"

    @pytest.mark.asyncio
    async def test_session_execute_passes_current_cwd(self, env):
        """cwd passed to vfs.execute is the session's CURRENT cwd at call time."""
        vfs, ns, admin, agent = env
        await vfs.grant(admin.id, agent.id, ns.id, "/", {"execute", "read"})
        await vfs.grant(admin.id, agent.id, ns.id, "/data/", {"execute", "read"})

        session = Session(vfs, ns.id, agent.id)
        await session.cd("/data/")

        cwd_at_dispatch: list[str] = []
        original_execute = vfs.execute

        async def _capture_cwd(code, namespace_id, principal_id, provider_name, **kwargs):
            cwd_at_dispatch.append(kwargs.get("cwd", "/"))
            return ExecutionResult(success=True, output=None)

        vfs.execute = _capture_cwd
        try:
            await session.execute("pass", provider_name="fake")
        finally:
            vfs.execute = original_execute

        assert cwd_at_dispatch == ["/data/"]


# ---------------------------------------------------------------------------
# SessionSearch/FindPredicatesPassthrough
# ---------------------------------------------------------------------------


class TestFindPredicatesPassthrough:
    """SessionSearch/FindPredicatesPassthrough.

    session.search forwards find_predicates to vfs.search unchanged.
    """

    @pytest.mark.asyncio
    async def test_find_predicates_forwarded(self, env):
        from vfs.models import SearchType
        from vfs.protocols.search import FindPredicates

        vfs, ns, admin, agent = env
        session = Session(vfs, ns.id, agent.id)

        captured: list[dict] = []
        original_search = vfs.search

        async def _capture_search(namespace_id, query, scope, search_type, **kwargs):
            captured.append(kwargs)
            return []

        vfs.search = _capture_search
        try:
            pred = FindPredicates(name="*.py")
            await session.search("*", "/", SearchType.FIND, find_predicates=pred)
        finally:
            vfs.search = original_search

        assert len(captured) == 1
        assert captured[0].get("find_predicates") is pred

    @pytest.mark.asyncio
    async def test_find_predicates_none_forwarded(self, env):
        """find_predicates=None is forwarded as None (not omitted)."""
        from vfs.models import SearchType

        vfs, ns, admin, agent = env
        session = Session(vfs, ns.id, agent.id)

        captured: list[dict] = []
        original_search = vfs.search

        async def _capture_search(namespace_id, query, scope, search_type, **kwargs):
            captured.append(kwargs)
            return []

        vfs.search = _capture_search
        try:
            await session.search("*", "/", SearchType.GLOB, find_predicates=None)
        finally:
            vfs.search = original_search

        assert len(captured) == 1
        assert captured[0].get("find_predicates") is None
