# Delta for anchored-editing

> New capability, promoted from the `execution` baseline (`AnchoredEditing`), which is
> removed there in this change's `execution` delta. The promoted contract is a redesign:
> stateless anchors with no per-`execute` `AnchorMap`, usable by any agent without a sandbox.
> An anchor is the line's **absolute index plus a short content-bound checksum**; the index
> is the collision-free locator, the checksum is an integrity/fabrication guard. Conflict
> policy is strict — an edit conflicts if the file changed at all since the anchors were read.
> Mechanism (checksum function/length, single-token concerns) lives in `design.md`.

## ADDED Requirements

### Requirement: AnchorIdentity

An anchor SHALL identify a line by the line's **absolute (file-relative) index** together with a short checksum bound to that line's index and content.
The absolute index SHALL be the sole source of line identity: it uniquely targets exactly one line — including lines whose text is identical to other lines (blank lines, repeated boilerplate) — with no dependence on a stored map.
The checksum SHALL bind the pair `(index, line_content)` so that a change to either without a corresponding re-read is detectable.
Anchors SHALL be reproducible purely from file content (stateless): an anchor obtained from one read remains usable in a later, independent operation, provided the file version is unchanged.

Serves: anchored-edit-anywhere

#### Scenario: IndexTargetsUniqueLineEvenInBoilerplate

- **GIVEN** a file containing a block of identical lines
- **WHEN** an anchor for one of those lines is resolved against the unchanged content
- **THEN** it targets exactly that line by its absolute index — identical neighbors do not make
  it ambiguous

#### Scenario: AnchorReproducibleAcrossIndependentCalls

- **GIVEN** an anchor returned by `read_anchored` in one call
- **WHEN** it is used by `edit_anchored` in a separate call with no shared in-memory state and the
  file version is unchanged
- **THEN** it resolves to the same line (statelessness — no server-side map is consulted)

#### Scenario: ChecksumBindsIndexToContent

- **GIVEN** an anchor `i:c` for line `i`
- **WHEN** the anchor's index is altered to a different line whose content differs, without
  recomputing the checksum
- **THEN** the checksum no longer matches the content at the new index (the pair is detectably
  inconsistent — see `AnchoredEditConflicts`)

### Requirement: AnchoredRead

`read_anchored(path, offset=None, limit=None)` SHALL return the file's content for the requested line range (the whole file when `offset`/`limit` are omitted), the file's current version, and a per-line anchor for each returned line.
Line indices in anchors SHALL be **absolute** (file-relative), identical whether the line is reached by a full read or a windowed read.
Content SHALL be decoded as strict UTF-8; undecodable content SHALL raise a structured decode error and yield no anchors.
Lines SHALL be split on `\n` only (`\r` retained), and the presence or absence of a trailing newline SHALL be preserved.

Serves: anchored-edit-anywhere, monty-code-mode

#### Scenario: ReadReturnsContentVersionAndAnchors

- **GIVEN** a UTF-8 text file of N lines at version V
- **WHEN** `read_anchored(path)` is called
- **THEN** the content, the version V, and N line anchors are returned

#### Scenario: WindowedReadUsesAbsoluteIndices

- **GIVEN** a file and a windowed read `read_anchored(path, offset=100, limit=10)`
- **WHEN** the returned anchors are inspected
- **THEN** their indices are the absolute file line numbers (100…109), not 0…9 — so they match a
  full read's anchors for the same lines

#### Scenario: EmptyAndSingleLineFiles

- **GIVEN** an empty file and a one-line file
- **WHEN** each is read
- **THEN** both are handled with no error per the `\n`-split model; the one-line file yields one
  anchor

#### Scenario: UndecodableContentRaises

- **GIVEN** a file whose content is not valid UTF-8
- **WHEN** `read_anchored(path)` is called
- **THEN** a structured decode error is raised and no anchors are produced

#### Scenario: CrlfAndTrailingNewlinePreserved

- **GIVEN** a file with CRLF endings and no trailing newline
- **WHEN** the content is read and later written back through an edit
- **THEN** the `\r` characters and the absence of a trailing newline are preserved

### Requirement: AnchoredEdit

`edit_anchored(path, hunks, expected_version)` SHALL accept **one or more** hunks — each `(start_anchor, end_anchor, replacement)` — and apply them **atomically** to the inclusive line ranges they identify, writing a single new version, **when the file's current version equals `expected_version`** (the version `read_anchored` returned).
The agent-facing result SHALL be success or failure (with the new version on success); it SHALL NOT return the file content or anchors — consistent with standard edit tools and to avoid re-emitting the document.

Serves: anchored-edit-anywhere

#### Scenario: SingleHunkApplies

- **GIVEN** anchors bounding lines 4–6 of a file at version V and `expected_version=V`
- **WHEN** `edit_anchored` replaces that range with two new lines
- **THEN** a new version V+1 is written with lines 4–6 replaced and all other lines intact, and
  the result reports success and the new version

#### Scenario: MultipleHunksAppliedAtomically

- **GIVEN** two non-overlapping hunks against the same `expected_version`
- **WHEN** `edit_anchored` is called with both
- **THEN** both are applied in a single new version; if either hunk fails to resolve, neither is
  applied (atomic)

#### Scenario: ResultCarriesNoContentOrAnchors

- **GIVEN** a successful edit
- **WHEN** the agent-facing result is inspected
- **THEN** it contains success and the new version number, and does not contain the file content
  or per-line anchors

### Requirement: AnchoredEditConflicts

`edit_anchored` SHALL fail with a typed conflict error and SHALL NOT write when any of the following holds: the file's current version differs from `expected_version`; an anchor's checksum does not match the content at its index (a fabricated anchor, an index transposition, or an anchor pasted from a different file); an anchor's index is out of range; a hunk's `end_anchor` resolves before its `start_anchor` (inverted range); or the target path is a tombstone.
The system SHALL NOT apply an edit to a guessed line.

Serves: anchored-edit-anywhere, governed-mount

#### Scenario: FileChangedSinceReadConflicts

- **GIVEN** anchors read at version V and a concurrent write advancing the file to V+1
- **WHEN** `edit_anchored` is invoked with `expected_version=V`
- **THEN** a conflict error is raised and no new version is written (the concurrent write is not
  overwritten)

#### Scenario: ChecksumMismatchConflicts

- **GIVEN** an anchor whose index/checksum pair does not match the current content at that index
  (e.g. an index transposed to an adjacent identical line, or an anchor from another file)
- **WHEN** `edit_anchored` is invoked with that anchor at the matching `expected_version`
- **THEN** a conflict error is raised and no write occurs (the edit is not applied to the wrong
  line)

#### Scenario: OutOfRangeOrInvertedConflicts

- **GIVEN** an anchor whose index exceeds the file length, or a hunk whose `end_anchor` resolves
  before its `start_anchor`
- **WHEN** `edit_anchored` is invoked
- **THEN** a conflict error is raised and no write occurs

#### Scenario: EditTombstonedFileConflicts

- **GIVEN** a path deleted (tombstoned) since the anchors were read
- **WHEN** `edit_anchored` is invoked
- **THEN** the edit fails (not-found / conflict) and no version is written

### Requirement: ConsistencyFloor

The correctness of an anchored edit SHALL depend only on the metadata store's single-record conditional-write guarantee — the contract floor that storage `MetadataCASSemantics` requires of every adapter — not on read-your-writes read freshness.
On a backend whose reads may be stale, a stale read SHALL manifest as a conflict, never as an edit applied to non-current content.

Serves: governed-mount, portable-sandboxes

#### Scenario: StaleReadManifestsAsConflict

- **GIVEN** a backend that returns a stale (older) version of a file from a read
- **WHEN** an anchored edit derived from that stale read is committed against the authoritative
  current version
- **THEN** the conditional write rejects it as a conflict; no edit is applied to non-current
  content

#### Scenario: ConcurrentEditsSerialize

- **GIVEN** two principals that both read a file at version V and edit it
- **WHEN** both commit
- **THEN** exactly one succeeds (V+1) and the other fails with a conflict (it must re-read);
  neither write is silently lost

### Requirement: AnchoredEditingStandaloneSurface

The anchored-editing surface (`read_anchored`, `edit_anchored`) SHALL be constructable bound to a `(namespace, principal)` context and invocable across independent calls without an execution sandbox.
The same capability SHALL back both the in-language `edit` verb exposed to the Monty sandbox and a standalone tool an agent framework can call directly.
Every operation SHALL enforce the principal's read/write permissions on the target path.
The surface SHALL signal failures by raising typed exceptions (a conflict error, `PermissionDeniedError`, `NotFoundError`, and the decode error); sandbox shell wrappers translate these to marshalable structured results.

Serves: anchored-edit-anywhere, monty-code-mode, just-bash-shell-tool

#### Scenario: StandaloneReadEditCycle

- **GIVEN** an anchored-editing surface bound to a `(namespace, principal)` with write
  permission, and no sandbox
- **WHEN** the caller invokes `read_anchored`, then `edit_anchored` with the returned version, as
  two separate calls
- **THEN** the edit is applied — the surface works outside any sandbox

#### Scenario: EditRequiresWritePermission

- **GIVEN** a principal with read but not write permission on a path
- **WHEN** `edit_anchored` is invoked on that path
- **THEN** `PermissionDeniedError` is raised and no version is written
