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
