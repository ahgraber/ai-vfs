# File Operations — Delta Spec

> Change: `phase2-storage`
> Date: 2026-05-22

## MODIFIED Requirements

### Requirement: MoveFile

> Previously: the move was specified as unconditionally atomic, which a best-effort no-op `transaction()` cannot guarantee.

The system SHALL move (rename) a file to a new path within the same namespace: the destination is created with the source's current content hash and the source receives a tombstone.
Version history is not transferred; the destination begins a new version chain.

On metadata stores providing atomic multi-document transactions (SQLite, Postgres, MongoDB replica sets) the move SHALL be fully atomic — neither a partial destination nor an unintended source tombstone is observable after a mid-operation failure.

On stores without atomic multi-document transactions (standalone MongoDB) the move SHALL order its writes destination-before-source.
A mid-operation failure on such a store is non-destructive in that **no version is ever lost** — history is append-only, so any prior destination content remains retrievable as a non-current version — but the move MAY be left **partially applied**: the destination's current version may already be advanced to the source's content while the source remains live.
For a new destination this manifests as a duplicate (both paths resolve to the source content); for an existing destination the destination's current pointer may advance without the source being tombstoned.
Callers SHALL treat move as non-atomic on best-effort stores and re-resolve on failure.

#### Scenario: MoveToNewPath

- **GIVEN** a file at /src/a.py at version 5
- **WHEN** a principal moves /src/a.py to /dst/a.py
- **THEN** /dst/a.py exists with the same content hash, and /src/a.py has a tombstone (is_deleted=True)

#### Scenario: MoveAtomicOnTransactionalStore

- **GIVEN** a move in progress on a store with atomic transactions (SQL or Mongo replica set)
- **WHEN** any failure occurs mid-operation
- **THEN** the system leaves neither a partial destination nor an unintended tombstone on the source

#### Scenario: MoveNonDestructiveOnBestEffortStore

- **GIVEN** a move in progress on standalone MongoDB (best-effort `transaction()`)
- **WHEN** a failure occurs after the destination is created but before the source is tombstoned
- **THEN** the file remains readable at both source and destination — no version is lost

#### Scenario: MoveToExistingPath

- **GIVEN** a file already exists at the destination path
- **WHEN** a principal moves to that path
- **THEN** the destination is overwritten (a new destination version) and the source receives a tombstone

#### Scenario: MoveToExistingPathPartialOnBestEffortStore

- **GIVEN** a file already exists at the destination on standalone MongoDB (best-effort `transaction()`)
- **WHEN** a failure occurs after the destination's new version is created but before the source is tombstoned
- **THEN** the destination's current content is the source's content, the source remains live, and the destination's prior content is still retrievable as a non-current version — no version is lost, but the move is partially applied

#### Scenario: MoveNonexistentSource

- **GIVEN** the source path does not exist
- **WHEN** a principal issues a move
- **THEN** a NotFoundError is raised and no destination record is created
