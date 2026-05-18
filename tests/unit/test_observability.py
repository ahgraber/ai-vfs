"""Tests for OTel tracing helpers and audit log."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from vfs.models import AuditEvent
from vfs.observability.audit import _current_trace_id, audit, audit_write
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
        await vfs.grant(admin.id, principal.id, ns.id, "/", {"read", "write", "delete"})
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
