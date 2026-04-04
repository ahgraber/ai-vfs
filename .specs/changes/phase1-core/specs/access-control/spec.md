# Access Control — Delta Spec

> Change: `phase1-core`
> Date: 2026-04-04

## MODIFIED: HumanFriendlyNames

**Previous behavior:** The names table mapped ULIDs to display names for all entity types.

**New behavior:** The names table maps any entity identifier (UUID4 or ULID, depending on the entity type's privacy classification) to display names.
The VFS API accepts either the raw identifier or a display name, resolving to the underlying identifier at the boundary.
The names table stores identifiers as opaque text regardless of format.

### Scenario: ResolveNameToULID

- **GIVEN** a namespace with ULID `"01JQX..."` and `display_name="my-workspace"`
- **WHEN** a name lookup for `"my-workspace"` is performed
- **THEN** the ULID `"01JQX..."` is returned

### Scenario: ResolveNameToUUID4

- **GIVEN** a principal with UUID4 `"550e8400-..."` and `display_name="agent-bob"`
- **WHEN** a name lookup for `"agent-bob"` is performed
- **THEN** the UUID4 `"550e8400-..."` is returned
