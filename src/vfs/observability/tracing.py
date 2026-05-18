"""OTel tracing and metrics helpers for VFS operations."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

from opentelemetry import metrics, trace

_tracer = trace.get_tracer("vfs")
_meter = metrics.get_meter("vfs")

op_counter = _meter.create_counter("vfs.operation.count", unit="1")
op_histogram = _meter.create_histogram("vfs.operation.duration", unit="ms")
blob_histogram = _meter.create_histogram("vfs.blob.size", unit="By")
search_candidate_histogram = _meter.create_histogram("vfs.search.candidates", unit="1")


@contextmanager
def vfs_span(operation: str, attrs: dict[str, Any], *, otel_enabled: bool):
    """Context manager that creates an OTel span when enabled."""
    if not otel_enabled:
        yield None
        return
    with _tracer.start_as_current_span(f"vfs.{operation}", attributes=attrs) as span:
        yield span


def record_op(operation: str, duration_ms: float, attrs: dict[str, Any], *, otel_enabled: bool) -> None:
    """Record operation count and duration metrics."""
    if not otel_enabled:
        return
    op_counter.add(1, {"vfs.operation": operation, **attrs})
    op_histogram.record(duration_ms, {"vfs.operation": operation, **attrs})


def record_blob_size(size: int, *, otel_enabled: bool) -> None:
    """Record blob size metric."""
    if not otel_enabled:
        return
    blob_histogram.record(size)


def record_search_candidates(count: int, attrs: dict[str, Any], *, otel_enabled: bool) -> None:
    """Record the number of permission-pruned candidates a search considered."""
    if not otel_enabled:
        return
    search_candidate_histogram.record(count, attrs)
