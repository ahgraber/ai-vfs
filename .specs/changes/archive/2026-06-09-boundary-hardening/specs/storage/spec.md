# Storage — Delta Spec

> Change: `boundary-hardening`
> Date: 2026-06-09

## ADDED Requirements

### Requirement: PrefixQueryLiteralMatching

The system SHALL treat path-prefix arguments to storage queries as **literal strings**, not as SQL LIKE patterns or regex expressions.
Any `%`, `_`, or `\` characters in a path prefix must be escaped before use in a SQL `LIKE` clause (using an `ESCAPE` clause), and before use in a MongoDB `$regex` query (using `re.escape`).

A file at path `/my_dir/report.txt` SHALL be returned when listing the prefix
`/my_dir/`, and SHALL NOT be returned when listing `/myXdir/` (where `X` is any single
character), even though the SQL LIKE pattern `/my_dir/%` would match `/myXdir/` if `_`
were treated as a wildcard.

#### Scenario: UnderscoreInPrefixMatchesLiterally

- **GIVEN** a file exists at `/my_dir/report.txt`
- **WHEN** the prefix `/my_dir/` is used to list files
- **THEN** `/my_dir/report.txt` is returned and `/myXdir/report.txt` is NOT returned

#### Scenario: PercentInPrefixMatchesLiterally

- **GIVEN** a file exists at `/data%2F/file.txt`
- **WHEN** the prefix `/data%2F/` is used to list files
- **THEN** `/data%2F/file.txt` is returned and only files under that exact prefix are returned

## MODIFIED Requirements

### Requirement: MetadataCASSemantics

> Previously: did not specify behaviour for the version-number uniqueness constraint
> under concurrent no-CAS writes.

The MetadataStore SHALL implement compare-and-swap semantics for version mutations.
SQL adapters SHALL use `WHERE version_number = ?` returning zero rows on mismatch.
NoSQL adapters SHALL use atomic find-and-update with version matching.

When two concurrent writers both attempt to insert the same `version_number` without an `expected_version` (no-CAS write), the store SHALL translate the resulting unique-constraint violation (`IntegrityError` for SQL, `DuplicateKeyError` for MongoDB) into a `VersionCollisionError` — distinct from `ConflictError`.
The VFS layer retries on `VersionCollisionError`; `ConflictError` from a CAS mismatch continues to propagate un-retried.

#### Scenario: CASConflictDetected

- **GIVEN** a file at version 5
- **WHEN** put_version is called with expected_version=3
- **THEN** a ConflictError is raised

#### Scenario: MongoCASConflict

- **GIVEN** a file at version 5 in MongoMetadataStore
- **WHEN** put_version is called with expected_version=3
- **THEN** a ConflictError is raised via `find_one_and_update` with version match returning no document

#### Scenario: NoCASVersionCollision

- **GIVEN** a file at version N
- **WHEN** two concurrent writers both call put_version with version_number=N+1 and no expected_version
- **THEN** the losing writer receives VersionCollisionError (not IntegrityError or DuplicateKeyError)
