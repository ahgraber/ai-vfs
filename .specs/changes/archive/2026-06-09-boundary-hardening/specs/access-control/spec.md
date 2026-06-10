# Access Control — Delta Spec

> Change: `boundary-hardening`
> Date: 2026-06-09

## MODIFIED Requirements

### Requirement: PathPrefixPermissions

> Previously: used naive `path.startswith(path_prefix)` for matching, which allows a
> grant on `/work` to cover `/workspace/file` (segment-boundary bypass).

The system SHALL evaluate permissions by matching the requested path against permission
entries' `path_prefix` fields using **segment-boundary precision**, with
most-specific-prefix-first (longest prefix) ordering.

A prefix P matches path X if and only if:

- `X == P` (exact match — covers single-file grants), or
- `X` starts with `P + "/"` when P does not end with `"/"`, or
- `X` starts with `P` when P already ends with `"/"`.

This ensures that a grant on `/work` covers `/work` and `/work/file.txt` but does **not** cover `/workspace/file.txt`.

The system SHALL validate that any `path_prefix` stored via `set_permission` or
`grant` is canonical (absolute and normpath-equal after stripping at most one trailing
`"/"`), raising `ValueError` otherwise.

#### Scenario: MostSpecificPrefixWins

- **GIVEN** principal has read-only on "/" and read-write on "/workspace/"
- **WHEN** the principal writes to "/workspace/file.txt"
- **THEN** the write is allowed (the /workspace/ rule is more specific)

#### Scenario: BroadRuleApplies

- **GIVEN** principal has read-only on "/" and read-write on "/workspace/"
- **WHEN** the principal writes to "/config.yaml"
- **THEN** the write is denied (only the broad "/" rule matches, which is read-only)

#### Scenario: SegmentBoundaryNotBypassed

- **GIVEN** principal has read-write on "/work"
- **WHEN** the principal attempts to read "/workspace/file.txt"
- **THEN** the read is denied (the "/work" grant does not cover "/workspace/")

#### Scenario: ExactGrantCoversFile

- **GIVEN** principal has read-write on "/work"
- **WHEN** the principal reads "/work"
- **THEN** the read is allowed (exact match on the grant path)

#### Scenario: DirectoryGrantCoversChildren

- **GIVEN** principal has read-write on "/work/"
- **WHEN** the principal reads "/work/file.txt"
- **THEN** the read is allowed (path falls under the /work/ subtree)
