"""Audit log helpers with OTel trace ID correlation."""

from __future__ import annotations

from datetime import datetime, timezone

from opentelemetry import trace as otel_trace
from ulid import ULID

from vfs.models import AuditEvent


def _current_trace_id() -> str | None:
    span = otel_trace.get_current_span()
    ctx = span.get_span_context()
    if ctx and ctx.trace_id != 0:
        return format(ctx.trace_id, "032x")
    return None


async def audit(meta_store, event: AuditEvent, *, audit_log_enabled: bool) -> None:
    """Persist an audit event if auditing is enabled."""
    if not audit_log_enabled:
        return
    event = event.model_copy(update={"trace_id": _current_trace_id()})
    await meta_store.append_audit_event(event)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def audit_write(
    meta_store,
    *,
    namespace_id: str,
    principal_id: str,
    path: str,
    version_id: str,
    audit_log_enabled: bool,
) -> None:
    """Record a write operation to the audit log."""
    event = AuditEvent(
        event_id=str(ULID()),
        timestamp=_now(),
        namespace_id=namespace_id,
        principal_id=principal_id,
        operation="write",
        path=path,
        version_id=version_id,
    )
    await audit(meta_store, event, audit_log_enabled=audit_log_enabled)


async def audit_delete(
    meta_store,
    *,
    namespace_id: str,
    principal_id: str,
    path: str,
    version_id: str,
    audit_log_enabled: bool,
) -> None:
    """Record a delete operation to the audit log."""
    event = AuditEvent(
        event_id=str(ULID()),
        timestamp=_now(),
        namespace_id=namespace_id,
        principal_id=principal_id,
        operation="delete",
        path=path,
        version_id=version_id,
    )
    await audit(meta_store, event, audit_log_enabled=audit_log_enabled)


async def audit_rollback(
    meta_store,
    *,
    namespace_id: str,
    principal_id: str,
    path: str,
    version_id: str,
    target_version_id: str,
    audit_log_enabled: bool,
) -> None:
    """Record a rollback operation to the audit log."""
    event = AuditEvent(
        event_id=str(ULID()),
        timestamp=_now(),
        namespace_id=namespace_id,
        principal_id=principal_id,
        operation="rollback",
        path=path,
        version_id=version_id,
        detail={"target_version_id": target_version_id},
    )
    await audit(meta_store, event, audit_log_enabled=audit_log_enabled)


async def audit_copy(
    meta_store,
    *,
    namespace_id: str,
    principal_id: str,
    src_path: str,
    dst_path: str,
    version_id: str,
    audit_log_enabled: bool,
) -> None:
    """Record a copy operation to the audit log."""
    event = AuditEvent(
        event_id=str(ULID()),
        timestamp=_now(),
        namespace_id=namespace_id,
        principal_id=principal_id,
        operation="copy",
        path=dst_path,
        version_id=version_id,
        detail={"src_path": src_path},
    )
    await audit(meta_store, event, audit_log_enabled=audit_log_enabled)


async def audit_move(
    meta_store,
    *,
    namespace_id: str,
    principal_id: str,
    src_path: str,
    dst_path: str,
    version_id: str,
    audit_log_enabled: bool,
) -> None:
    """Record a move operation to the audit log."""
    event = AuditEvent(
        event_id=str(ULID()),
        timestamp=_now(),
        namespace_id=namespace_id,
        principal_id=principal_id,
        operation="move",
        path=dst_path,
        version_id=version_id,
        detail={"src_path": src_path},
    )
    await audit(meta_store, event, audit_log_enabled=audit_log_enabled)


async def audit_permission_change(
    meta_store,
    *,
    namespace_id: str,
    principal_id: str,
    target_principal_id: str,
    path_prefix: str,
    operations: set[str],
    audit_log_enabled: bool,
) -> None:
    """Record a permission grant or modification to the audit log."""
    event = AuditEvent(
        event_id=str(ULID()),
        timestamp=_now(),
        namespace_id=namespace_id,
        principal_id=principal_id,
        operation="permission_change",
        detail={
            "target_principal_id": target_principal_id,
            "path_prefix": path_prefix,
            "operations": sorted(operations),
        },
    )
    await audit(meta_store, event, audit_log_enabled=audit_log_enabled)


async def audit_gc_run(
    meta_store,
    *,
    namespace_id: str | None,
    versions_reclaimed: int,
    blobs_reclaimed: int,
    audit_log_enabled: bool,
) -> None:
    """Record a garbage collection run to the audit log."""
    event = AuditEvent(
        event_id=str(ULID()),
        timestamp=_now(),
        namespace_id=namespace_id or "__system__",
        principal_id="__system__",
        operation="gc_run",
        detail={
            "versions_reclaimed": versions_reclaimed,
            "blobs_reclaimed": blobs_reclaimed,
        },
    )
    await audit(meta_store, event, audit_log_enabled=audit_log_enabled)
