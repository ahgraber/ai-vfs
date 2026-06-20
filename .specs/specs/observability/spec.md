# Observability — Spec

> **Why (trust thesis):** the append-only audit log is the _accountability_ facet of `NORTH-STAR.md` bet #2 (trust) — every agent state-change is attributable after the fact. The rationale lives in the north star; this spec is the contract.

## Requirements

### Requirement: OTelSpansOnAllOperations

The system SHALL create an OTel span for every VFS operation (read, write, delete, stat, list, search, versions, rollback).
Spans SHALL carry attributes: namespace_id, path, principal_id.

#### Scenario: WriteSpanAttributes

- **GIVEN** OTel is configured
- **WHEN** a write operation completes
- **THEN** a span named "vfs.write" exists with attributes vfs.path, vfs.namespace_id, vfs.version_number, vfs.content_hash, vfs.blob_size_bytes

#### Scenario: ChildSpans

- **GIVEN** a write operation executes
- **WHEN** sub-operations occur (metadata.get_file, blob.put, search.index)
- **THEN** each sub-operation is a child span of the vfs.write span

### Requirement: OTelMetrics

The system SHOULD emit OTel metrics: operation count (by operation, namespace),
operation duration histogram, blob size histogram, and search candidate count.

#### Scenario: OperationCountMetric

- **GIVEN** 5 read operations and 3 write operations occur
- **WHEN** metrics are collected
- **THEN** vfs.operation.count shows 5 for read and 3 for write

### Requirement: OTelContextPropagation

The system SHALL support OTel context propagation so that agent framework traces can parent-link to VFS spans.

#### Scenario: ParentSpanLinked

- **GIVEN** an agent framework creates a parent span
- **WHEN** the agent calls vfs.read within that span context
- **THEN** the vfs.read span is a child of the agent framework's span

### Requirement: NoOpWhenDisabled

The system SHALL operate correctly when OTel is not configured or is disabled.
Spans SHALL be no-ops; no errors SHALL occur.

#### Scenario: OTelDisabled

- **GIVEN** otel_enabled=False in config
- **WHEN** any VFS operation executes
- **THEN** no spans are created and no errors occur

### Requirement: AuditLogStateChanges

The system SHALL append an audit event to the metadata store for every
state-changing operation: write, delete, rollback, permission change, and GC run.

#### Scenario: WriteAudited

- **GIVEN** audit_log_enabled=True
- **WHEN** a write operation completes
- **THEN** an AuditEvent is persisted with operation="write", path, version_id, and detail containing content_hash and size

#### Scenario: ReadNotAudited

- **GIVEN** audit_log_enabled=True
- **WHEN** a read operation completes
- **THEN** no audit event is created (reads are OTel-only)

### Requirement: AuditLogAppendOnly

The system SHALL NOT update or delete audit events.
GC MAY archive old audit entries but SHALL NOT delete them.

> **Deferred:** No archival or rotation mechanism exists; under sustained agent write load
> the audit table grows unbounded. An archival strategy is deferred.

#### Scenario: AuditImmutable

- **GIVEN** an audit event was created for a write
- **WHEN** the file is later deleted
- **THEN** the original write audit event remains unchanged

### Requirement: AuditOTelCorrelation

The system SHALL include the current OTel trace_id in audit events when
an active trace context exists, enabling correlation between audit records
and execution traces.

#### Scenario: TraceIDInAuditEvent

- **GIVEN** an active OTel trace context with trace_id "abc123..."
- **WHEN** a write operation creates an audit event
- **THEN** the audit event's trace_id field contains "abc123..."

#### Scenario: NoTraceContext

- **GIVEN** no active OTel trace context
- **WHEN** a write operation creates an audit event
- **THEN** the audit event's trace_id field is None
