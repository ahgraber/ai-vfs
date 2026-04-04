# File Operations — Delta Spec

> Change: `phase1-core`
> Date: 2026-04-04

## MODIFIED: ULIDIdentifiers

**Previous behavior:** All entities used ULIDs as primary identifiers.

**New behavior:** Identifier type is chosen based on temporal-information exposure risk:

- **UUID4** (fully random, no embedded timestamp): any person-related entity, or any entity whose ID may be exposed externally and where leaking temporal information — concrete (exact creation time) or relative (creation ordering between two entities) — could give an attacker exploitable information.
  Current example: `Principal.id`.

- **ULID** (time-sortable, timestamp in high bits): file-system entities and internal metadata where temporal sortability aids debugging and log correlation, and the IDs are not expected to leak person-related timing signals.
  Current examples: `Namespace.id`, `VersionMeta.id`, `Permission.id`, `AuditEvent.event_id`.

The per-file monotonic `version_number` integer continues to serve as the human-facing version identifier.

When a new entity type is introduced, the implementer SHALL evaluate whether a
time-sortable ID would expose concrete or relative timing information to an external
caller, and use UUID4 if so.

### Scenario: PrincipalIdIsFullyRandom

- **GIVEN** a new principal is created
- **WHEN** the principal record is inspected
- **THEN** `principal.id` is a UUID4 string with no embedded timestamp, so that neither
  the creation time nor the relative creation order of two principals can be inferred
  from their IDs

### Scenario: FileEntityIdIsULID

- **GIVEN** a new namespace or version is created
- **WHEN** the entity record is inspected
- **THEN** the entity `id` is a 26-character ULID string encoding the creation timestamp

### Scenario: VersionDualIdentifier

- **GIVEN** a new version is created
- **WHEN** the version record is inspected
- **THEN** it has both a globally unique ULID `id` and a per-file `version_number` integer
