"""Tests for OTel tracing helpers and audit log."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from vfs.models import AuditEvent
from vfs.observability.audit import _current_trace_id, audit, audit_execute, audit_write
from vfs.observability.tracing import (
    blob_histogram,
    op_counter,
    op_histogram,
    record_blob_size,
    record_op,
    record_search_candidates,
    search_candidate_histogram,
    vfs_span,
)


class TestOTelTracing:
    """Task 11: OTel tracing helpers."""

    def test_span_created_when_enabled(self):
        with patch("vfs.observability.tracing._tracer") as mock_tracer:
            mock_span = MagicMock()
            mock_tracer.start_as_current_span.return_value.__enter__ = MagicMock(return_value=mock_span)
            mock_tracer.start_as_current_span.return_value.__exit__ = MagicMock(return_value=False)
            with vfs_span("write", {"path": "/a"}, otel_enabled=True) as span:
                assert span is not None
            mock_tracer.start_as_current_span.assert_called_once()
            assert mock_tracer.start_as_current_span.call_args[0][0] == "vfs.write"

    def test_no_span_when_disabled(self):
        with patch("vfs.observability.tracing._tracer") as mock_tracer:
            with vfs_span("write", {"path": "/a"}, otel_enabled=False) as span:
                assert span is None
            mock_tracer.start_as_current_span.assert_not_called()

    def test_metrics_counter_incremented(self):
        with patch.object(op_counter, "add") as mock_add:
            record_op("write", 10.0, {}, otel_enabled=True)
            mock_add.assert_called_once()
            args = mock_add.call_args[0]
            assert args[0] == 1
            assert args[1].get("vfs.operation") == "write"

    def test_metrics_duration_histogram_recorded(self):
        with patch.object(op_histogram, "record") as mock_record:
            record_op("write", 15.5, {}, otel_enabled=True)
            mock_record.assert_called_once()
            assert mock_record.call_args[0][0] == 15.5

    def test_metrics_blob_size_histogram_recorded(self):
        with patch.object(blob_histogram, "record") as mock_record:
            record_blob_size(1024, otel_enabled=True)
            mock_record.assert_called_once_with(1024)

    def test_search_candidate_count_recorded(self):
        with patch.object(search_candidate_histogram, "record") as mock_record:
            record_search_candidates(7, {"vfs.namespace": "ns1"}, otel_enabled=True)
            mock_record.assert_called_once()
            args = mock_record.call_args[0]
            assert args[0] == 7
            assert args[1].get("vfs.namespace") == "ns1"

    def test_search_candidate_count_no_op_when_disabled(self):
        with patch.object(search_candidate_histogram, "record") as mock_record:
            record_search_candidates(7, {"vfs.namespace": "ns1"}, otel_enabled=False)
            mock_record.assert_not_called()

    @pytest.mark.asyncio
    async def test_search_emits_candidate_count_metric(self, otel_vfs_instance):
        """End-to-end: VFS.search records the candidate histogram."""
        vfs = otel_vfs_instance
        ns = await vfs.create_namespace("ws-metric", "admin")
        admin = await vfs.create_principal("admin-metric")
        await vfs.bootstrap_admin(admin.id, ns.id)
        principal = await vfs.create_principal("agent-metric")
        await vfs.grant(admin.id, principal.id, ns.id, "/", {"read", "write"})
        await vfs.write(ns.id, "/a.py", b"x", principal_id=principal.id)
        await vfs.write(ns.id, "/b.py", b"x", principal_id=principal.id)

        from vfs.models import SearchType

        with patch.object(search_candidate_histogram, "record") as mock_record:
            await vfs.search(ns.id, "*.py", "/", SearchType.GLOB, principal_id=principal.id)
        mock_record.assert_called_once()
        count, attrs = mock_record.call_args[0]
        assert count == 2
        assert attrs.get("vfs.namespace") == ns.id
        assert attrs.get("vfs.search_type") == "glob"


class TestContextPropagation:
    """OTelContextPropagation: vfs spans inherit an active parent span's context.

    Uses the OTel SDK's `InMemorySpanExporter` so the parent-child relationship can be
    observed on finished spans. The proxy tracer created at `tracing.py` import time
    dispatches to whatever global provider is active, so installing a real SDK provider
    here is sufficient.
    """

    @pytest.mark.asyncio
    async def test_vfs_span_links_to_parent_span(self, otel_vfs_instance):
        from opentelemetry import trace as otel_trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )

        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        prior_provider = otel_trace._TRACER_PROVIDER  # type: ignore[attr-defined]
        otel_trace._TRACER_PROVIDER = provider  # type: ignore[attr-defined]
        try:
            vfs = otel_vfs_instance
            ns = await vfs.create_namespace("ws-trace", "admin")
            admin = await vfs.create_principal("admin-trace")
            await vfs.bootstrap_admin(admin.id, ns.id)
            principal = await vfs.create_principal("agent-trace")
            await vfs.grant(admin.id, principal.id, ns.id, "/", {"read", "write"})

            tracer = provider.get_tracer("test")
            with tracer.start_as_current_span("parent-span") as parent:
                parent_ctx = parent.get_span_context()
                assert parent_ctx.trace_id != 0, "parent span should have a real SDK trace_id"
                await vfs.write(ns.id, "/a.py", b"data", principal_id=principal.id)

            finished = exporter.get_finished_spans()
            write_spans = [s for s in finished if s.name == "vfs.write"]
            assert write_spans, f"no vfs.write span captured; saw: {[s.name for s in finished]}"
            for s in write_spans:
                assert s.context.trace_id == parent_ctx.trace_id, (
                    f"vfs.write trace_id={s.context.trace_id:x} != parent {parent_ctx.trace_id:x}"
                )
                assert s.parent is not None, "vfs.write should have a parent context"
                assert s.parent.span_id == parent_ctx.span_id, "vfs.write parent span_id should match the outer span"
        finally:
            otel_trace._TRACER_PROVIDER = prior_provider  # type: ignore[attr-defined]

    def test_no_error_when_otel_not_configured(self):
        # Should not raise even without an SDK
        with vfs_span("read", {}, otel_enabled=True):
            pass
        record_op("read", 1.0, {}, otel_enabled=True)
        record_blob_size(100, otel_enabled=True)


class TestAuditLog:
    """Task 12: Audit log helper."""

    @pytest.mark.asyncio
    async def test_audit_write_creates_event(self):
        mock_store = AsyncMock()
        await audit_write(
            mock_store,
            namespace_id="ns1",
            principal_id="p1",
            path="/a.py",
            version_id="v1",
            audit_log_enabled=True,
        )
        mock_store.append_audit_event.assert_awaited_once()
        event = mock_store.append_audit_event.call_args[0][0]
        assert event.operation == "write"

    def test_no_audit_read_helper(self):
        """Reads are not audited — no audit_read function exists."""
        import vfs.observability.audit as audit_mod

        assert not hasattr(audit_mod, "audit_read")

    @pytest.mark.asyncio
    async def test_audit_execute_records_success(self):
        mock_store = AsyncMock()
        await audit_execute(
            mock_store,
            namespace_id="ns1",
            principal_id="p1",
            path="/",
            provider="monty",
            success=True,
            audit_log_enabled=True,
        )
        event = mock_store.append_audit_event.call_args[0][0]
        assert event.operation == "execute"
        assert event.path == "/"
        assert event.detail["provider"] == "monty"
        assert event.detail["outcome"] == "success"
        assert "error_type" not in event.detail

    @pytest.mark.asyncio
    async def test_audit_execute_records_failure_with_error_type(self):
        mock_store = AsyncMock()
        await audit_execute(
            mock_store,
            namespace_id="ns1",
            principal_id="p1",
            path="/work",
            provider="just-bash",
            success=False,
            error_type="nonzero_exit",
            audit_log_enabled=True,
        )
        event = mock_store.append_audit_event.call_args[0][0]
        assert event.operation == "execute"
        assert event.detail["outcome"] == "failure"
        assert event.detail["error_type"] == "nonzero_exit"

    @pytest.mark.asyncio
    async def test_trace_id_in_audit(self):
        """Active OTel span → trace_id populated."""
        mock_store = AsyncMock()
        event = AuditEvent(
            event_id="e1",
            timestamp=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            namespace_id="ns1",
            principal_id="p1",
            operation="write",
        )
        with patch("vfs.observability.audit._current_trace_id", return_value="abc123"):
            await audit(mock_store, event, audit_log_enabled=True)
        stored = mock_store.append_audit_event.call_args[0][0]
        assert stored.trace_id == "abc123"

    @pytest.mark.asyncio
    async def test_no_trace_id_without_context(self):
        mock_store = AsyncMock()
        event = AuditEvent(
            event_id="e1",
            timestamp=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            namespace_id="ns1",
            principal_id="p1",
            operation="write",
        )
        # No span active → trace_id from _current_trace_id is None
        await audit(mock_store, event, audit_log_enabled=True)
        stored = mock_store.append_audit_event.call_args[0][0]
        assert stored.trace_id is None

    @pytest.mark.asyncio
    async def test_audit_disabled(self):
        mock_store = AsyncMock()
        await audit_write(
            mock_store,
            namespace_id="ns1",
            principal_id="p1",
            path="/a.py",
            version_id="v1",
            audit_log_enabled=False,
        )
        mock_store.append_audit_event.assert_not_awaited()


class TestSpanAttributes:
    """Verify every VFS public method opens a span carrying namespace, path, and principal_id."""

    @pytest.mark.asyncio
    async def test_all_vfs_operations_carry_principal_id_on_spans(self, otel_vfs_instance):
        from collections import defaultdict

        vfs = otel_vfs_instance
        ns = await vfs.create_namespace("ws-span", "admin")
        admin = await vfs.create_principal("admin-span")
        await vfs.bootstrap_admin(admin.id, ns.id)
        principal = await vfs.create_principal("agent-span")
        await vfs.grant(admin.id, principal.id, ns.id, "/", {"read", "write", "delete", "execute"})
        # Pre-populate state required by read/list/stat/delete/copy/move/versions/rollback/search.
        await vfs.write(ns.id, "/a.py", b"v1", principal_id=principal.id)
        await vfs.write(ns.id, "/a.py", b"v2", principal_id=principal.id)

        captured: list[tuple[str, dict]] = []

        def _capture(name, attributes=None, **kwargs):
            captured.append((name, dict(attributes or {})))
            from unittest.mock import MagicMock

            cm = MagicMock()
            cm.__enter__ = MagicMock(return_value=MagicMock())
            cm.__exit__ = MagicMock(return_value=False)
            return cm

        with patch("vfs.observability.tracing._tracer.start_as_current_span", side_effect=_capture):
            # Drive every public VFS method that opens a span.
            await vfs.stat(ns.id, "/a.py", principal_id=principal.id)
            await vfs.list(ns.id, "/", principal_id=principal.id, recursive=True)
            await vfs.write(ns.id, "/b.py", b"new", principal_id=principal.id)
            await vfs.read(ns.id, "/a.py", principal_id=principal.id)
            await vfs.copy(ns.id, "/a.py", "/copy_dst.py", principal_id=principal.id)
            await vfs.move(ns.id, "/b.py", "/move_dst.py", principal_id=principal.id)
            await vfs.versions(ns.id, "/a.py", principal_id=principal.id)
            await vfs.rollback(ns.id, "/a.py", 1, principal_id=principal.id)
            await vfs.delete(ns.id, "/a.py", principal_id=principal.id)
            from vfs.models import SearchType

            await vfs.search(ns.id, "*.py", "/", SearchType.GLOB, principal_id=principal.id)

            # Drive vfs.execute with a fake provider so no optional extra is needed.
            from vfs.execution import registry
            from vfs.protocols.execution import ExecutionResult

            class _FakeProvider:
                async def execute(self, code, fs_ops, fs_port, resource_limits):  # noqa: ARG002
                    return ExecutionResult(success=True, output=None)

            with patch.object(registry, "resolve_execution_provider", return_value=_FakeProvider()):
                await vfs.execute("noop", ns.id, principal.id, "fake", cwd="/")

        # Group captured spans by operation name.
        by_op: dict[str, list[dict]] = defaultdict(list)
        for name, attrs in captured:
            by_op[name].append(attrs)

        expected_ops = {
            "vfs.stat",
            "vfs.list",
            "vfs.write",
            "vfs.read",
            "vfs.copy",
            "vfs.move",
            "vfs.versions",
            "vfs.rollback",
            "vfs.delete",
            "vfs.search",
            "vfs.execute",
        }
        missing = expected_ops - set(by_op)
        assert not missing, f"VFS ops missing span coverage: {missing}"

        for op in expected_ops:
            for attrs in by_op[op]:
                assert attrs.get("vfs.namespace") == ns.id, f"{op} missing/wrong namespace attr"
                assert "vfs.path" in attrs, f"{op} missing path attr"
                assert attrs.get("vfs.principal_id") == principal.id, (
                    f"{op} missing or wrong principal_id attr: {attrs}"
                )


class _FakeExecProvider:
    """Minimal ExecutionProvider for observability tests (no optional extra needed).

    When ``inner_write`` is set, ``execute`` performs a write through the injected
    ``FsOperations`` so an inner ``vfs.write`` span/audit event is produced.
    """

    def __init__(self, inner_write: str | None = None):
        self._inner_write = inner_write

    async def execute(self, code, fs_ops, fs_port, resource_limits):  # noqa: ARG002
        from vfs.protocols.execution import ExecutionResult

        if self._inner_write is not None:
            await fs_ops.write(self._inner_write, b"data")
        return ExecutionResult(success=True, output=None)


def _install_sdk_exporter():
    """Install an in-memory SDK tracer provider; return (exporter, restore_fn).

    OpenTelemetry's module-level ``ProxyTracer`` (``tracing._tracer``) caches the
    first real provider it resolves in ``_real_tracer`` and never re-resolves. An
    earlier test that installed a provider therefore poisons this one — spans go to
    the stale (torn-down) provider and never reach ``exporter``. Clearing the cache
    on install forces re-resolution against the provider just set; clearing it on
    restore leaves no stale delegate for the next test.
    """
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from vfs.observability import tracing

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    prior = otel_trace._TRACER_PROVIDER  # type: ignore[attr-defined]
    otel_trace._TRACER_PROVIDER = provider  # type: ignore[attr-defined]
    tracing._tracer._real_tracer = None  # type: ignore[attr-defined]  # force re-resolve to `provider`

    def _restore():
        otel_trace._TRACER_PROVIDER = prior  # type: ignore[attr-defined]
        tracing._tracer._real_tracer = None  # type: ignore[attr-defined]

    return exporter, _restore


def _spy_audit(vfs):
    """Wrap the store's append_audit_event with a capturing spy; return the events list."""
    events: list = []
    original = vfs._meta.append_audit_event

    async def _spy(event):
        events.append(event)
        return await original(event)

    vfs._meta.append_audit_event = _spy
    return events


async def _bootstrap(vfs, ns_name, perms):
    ns = await vfs.create_namespace(ns_name, "admin")
    admin = await vfs.create_principal(f"admin-{ns_name}")
    await vfs.bootstrap_admin(admin.id, ns.id)
    agent = await vfs.create_principal(f"agent-{ns_name}")
    await vfs.grant(admin.id, agent.id, ns.id, "/", perms)
    return ns, agent


class TestExecuteObservabilityContract:
    """OTelSpansOnAllOperations + AuditLogStateChanges for vfs.execute."""

    @pytest.mark.asyncio
    async def test_execute_span_parents_inner_write(self, otel_vfs_instance):
        """ExecuteSpanParentsInnerOperations: the inner vfs.write span is a descendant."""
        from vfs.execution import registry

        exporter, restore = _install_sdk_exporter()
        try:
            vfs = otel_vfs_instance
            ns, agent = await _bootstrap(vfs, "exectrace", {"read", "write", "execute"})
            with patch.object(
                registry, "resolve_execution_provider", return_value=_FakeExecProvider(inner_write="/inner.txt")
            ):
                await vfs.execute("noop", ns.id, agent.id, "fake", cwd="/")

            finished = exporter.get_finished_spans()
            exec_spans = [s for s in finished if s.name == "vfs.execute"]
            write_spans = [s for s in finished if s.name == "vfs.write"]
            assert exec_spans and write_spans
            exec_span = exec_spans[0]
            assert any(w.context.trace_id == exec_span.context.trace_id for w in write_spans)
            assert any(w.parent is not None and w.parent.span_id == exec_span.context.span_id for w in write_spans), (
                "inner vfs.write should be a child of the vfs.execute span"
            )
        finally:
            restore()

    @pytest.mark.asyncio
    async def test_execute_creates_no_span_when_otel_disabled(self, otel_vfs_instance):
        """NoOpWhenDisabled regression for execute: no span, no error."""
        from vfs.execution import registry

        vfs = otel_vfs_instance
        vfs._config.otel_enabled = False
        ns, agent = await _bootstrap(vfs, "execnoop", {"read", "write", "execute"})

        captured: list[str] = []

        def _capture(name, attributes=None, **kwargs):
            captured.append(name)
            from unittest.mock import MagicMock

            cm = MagicMock()
            cm.__enter__ = MagicMock(return_value=MagicMock())
            cm.__exit__ = MagicMock(return_value=False)
            return cm

        with (
            patch("vfs.observability.tracing._tracer.start_as_current_span", side_effect=_capture),
            patch.object(registry, "resolve_execution_provider", return_value=_FakeExecProvider()),
        ):
            result = await vfs.execute("noop", ns.id, agent.id, "fake", cwd="/")

        assert result.success is True
        assert "vfs.execute" not in captured

    @pytest.mark.asyncio
    async def test_execute_and_inner_write_share_trace_and_both_audited(self, otel_vfs_instance):
        """ExecuteInnerWritesIndependentlyAudited: write + execute events, one shared trace_id."""
        from vfs.execution import registry

        exporter, restore = _install_sdk_exporter()
        try:
            vfs = otel_vfs_instance
            ns, agent = await _bootstrap(vfs, "execaudit", {"read", "write", "execute"})
            events = _spy_audit(vfs)
            with patch.object(
                registry, "resolve_execution_provider", return_value=_FakeExecProvider(inner_write="/inner.txt")
            ):
                await vfs.execute("noop", ns.id, agent.id, "fake", cwd="/")

            exec_events = [e for e in events if e.operation == "execute"]
            write_events = [e for e in events if e.operation == "write"]
            assert len(exec_events) == 1
            assert write_events, "the inner write must be audited independently"
            assert exec_events[0].trace_id is not None
            assert write_events[0].trace_id == exec_events[0].trace_id
        finally:
            restore()

    @pytest.mark.asyncio
    async def test_tier1_permission_denial_not_audited(self, otel_vfs_instance):
        """A Tier-1 execute-permission denial raises and audits nothing (no code ran)."""
        from vfs.errors import PermissionDeniedError
        from vfs.execution import registry

        vfs = otel_vfs_instance
        ns, agent = await _bootstrap(vfs, "execdeny", {"read", "write"})  # no execute
        events = _spy_audit(vfs)
        with (
            patch.object(registry, "resolve_execution_provider", return_value=_FakeExecProvider()),
            pytest.raises(PermissionDeniedError),
        ):
            await vfs.execute("noop", ns.id, agent.id, "fake", cwd="/")
        assert not [e for e in events if e.operation == "execute"]

    @pytest.mark.asyncio
    async def test_execute_not_audited_when_disabled(self, otel_vfs_instance):
        """No execute audit event is persisted when audit_log_enabled is False."""
        from vfs.execution import registry

        vfs = otel_vfs_instance
        vfs._config.audit_log_enabled = False
        ns, agent = await _bootstrap(vfs, "execauditoff", {"read", "write", "execute"})
        events = _spy_audit(vfs)
        with patch.object(registry, "resolve_execution_provider", return_value=_FakeExecProvider()):
            await vfs.execute("noop", ns.id, agent.id, "fake", cwd="/")
        assert not [e for e in events if e.operation == "execute"]


class TestCopyMoveAuditRegression:
    """Pins the already-implemented copy/move audit events to the now-explicit contract."""

    @pytest.mark.asyncio
    async def test_copy_audited(self, otel_vfs_instance):
        vfs = otel_vfs_instance
        ns, agent = await _bootstrap(vfs, "copyaud", {"read", "write", "delete"})
        await vfs.write(ns.id, "/src.txt", b"x", principal_id=agent.id)
        events = _spy_audit(vfs)
        new_ver = await vfs.copy(ns.id, "/src.txt", "/dst.txt", principal_id=agent.id)
        copy_events = [e for e in events if e.operation == "copy"]
        assert len(copy_events) == 1
        assert copy_events[0].path == "/dst.txt"
        assert copy_events[0].version_id == new_ver.id
        assert copy_events[0].detail.get("src_path") == "/src.txt"

    @pytest.mark.asyncio
    async def test_move_audited_exactly_once(self, otel_vfs_instance):
        vfs = otel_vfs_instance
        ns, agent = await _bootstrap(vfs, "moveaud", {"read", "write", "delete"})
        await vfs.write(ns.id, "/src.txt", b"x", principal_id=agent.id)
        events = _spy_audit(vfs)
        new_ver = await vfs.move(ns.id, "/src.txt", "/dst.txt", principal_id=agent.id)
        move_events = [e for e in events if e.operation == "move"]
        assert len(move_events) == 1
        assert move_events[0].path == "/dst.txt"
        assert move_events[0].version_id == new_ver.id
        assert move_events[0].detail.get("src_path") == "/src.txt"
