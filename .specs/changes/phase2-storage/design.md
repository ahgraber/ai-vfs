# Design: Phase 2 (Storage) — Production Metadata and Blob Adapters

## Context

Phase 1 shipped the local-only profile: `SQLiteMetadataStore`, `LocalFSBlobStore` (with an optional `cached_blob` wrapper), and a `GarbageCollector` that applies only `max_recent_versions`.
The ports already exist and are the unification layer:

- `MetadataStore` ([src/vfs/protocols/metadata.py](../../../src/vfs/protocols/metadata.py)) —
  file/version CRUD, permissions, audit, `update_search_meta`, `list_reclaimable_versions`,
  `transaction()`.
- `BlobStore` ([src/vfs/protocols/blob.py](../../../src/vfs/protocols/blob.py)) —
  `put`/`get`/`delete`/`exists`/`list_hashes`, streaming stubs.

This change adds production adapters behind those ports **without** adding a cross-store ORM or coupling search to the metadata backend.
Constraints carried from the design docs:

- The blob store has no concept of files/paths/versions; the user-facing filesystem is materialized entirely from metadata.
  Blob layout is an internal storage-efficiency choice with no user-facing effect.
- Optional dependencies (`asyncpg`, `motor`, `aiobotocore`) must not be imported by the
  VFS core; each adapter is importable only when its extra is installed.
- `search_meta` is persisted as an opaque manifest field.
  This change does not define its shape — that is `phase2-search`'s `SearchArtifact` envelope.
  Adapters store and return it verbatim.

Decisions are ordered by build dependency, matching `proposal.md` Scope and `tasks.md`.

## Decisions

### Decision: SQL adapters via SQLAlchemy Core + Alembic (not raw asyncpg, not SQLModel)

**Chosen:** Use `sqlalchemy.ext.asyncio` **Core** (Table/`select()`/`insert()`/ `Connection.execute()` — no ORM session, no `relationship()`, no declarative base) with `aiosqlite` and `asyncpg` dialects sharing one schema definition, and Alembic for versioned migrations.
Re-express `SQLiteMetadataStore` on Core so both SQL backends share the schema and CAS logic.
Mongo stays a fully separate path (Motor directly).

Because the default SQLite profile is itself on Core, `sqlalchemy` and `alembic` are **core dependencies**, not optional extras — the base install must include them.
The `postgres` extra adds only the `asyncpg` driver on top.
This is the deliberate cost of the shared-schema decision: a heavier base footprint in exchange for one SQL schema/CAS implementation across SQLite and Postgres rather than two divergent ones.

**Rationale:** Domain models live in `vfs/models.py`; SQLModel would couple table definitions to domain models or duplicate them.
Raw `asyncpg` means two divergent SQL implementations (separate DDL + row-mapping for SQLite vs Postgres); Core gives one schema and named column access.
`CREATE TABLE IF NOT EXISTS` cannot express future `ALTER TABLE`; Alembic provides upgrade/downgrade paths once the schema evolves post-release.

**Alternatives considered:**

- Raw `asyncpg`: divergent DDL/row-mapping across the two SQL backends; no migration tooling.
- SQLModel/full ORM: couples DB tables to domain models; pulls in session/identity-map
  machinery this design avoids.

### Decision: S3 object keys use the local-FS sharded layout `{hash[0:2]}/{hash[2:4]}/{hash}`

**Chosen:** Key S3 blobs as `{prefix}/{hash[0:2]}/{hash[2:4]}/{hash}`, mirroring `LocalFSBlobStore._path`.

**Rationale:** Write distribution is a non-issue on every target backend because BLAKE3 content-hash keys are uniformly high-entropy — S3 (auto-partitioning since 2018), MinIO (siphash of the full key), Ceph RGW (CRUSH on the full name; bucket index auto-reshards), and Garage (consistent hashing) all place by full-key hash; Azure's range partitioning only hot-spots on _sequential_ names, which a content hash is not.
The sharded layout instead bounds **objects-per-prefix for listing/scanning**, where backends differ: MinIO targets ~10K objects/prefix on modest hardware and Azure recommends a hash prefix for small-blob listing.
Listing is GC-only (background, non-hot-path), so this is defensive, but the cost is near-zero (same key-derivation as local FS) and keeps the two adapters symmetric.
Layout has no user-facing effect — the filesystem is materialized from metadata; the blob layer is opaque-by-hash regardless.

**Alternatives considered:**

- Flat keys `{prefix}/{hash}`: simplest and write-distribution-identical, but a single
  prefix accumulates every blob, bumping MinIO's per-prefix listing baseline at scale.

### Decision: auto-enable the blob cache for remote schemes

**Chosen:** When `blob_cache_enabled is None` (auto), wrap the blob store in the `diskcache` layer for `s3://` and leave `file:///` unwrapped.
An explicit value always wins.

**Rationale:** Resolves the Phase 1 baseline forward-reference.
Remote round-trips dominate read latency for S3; immutable content-hash keys make cache invalidation a non-issue.
Local FS reads are already fast, so caching them only doubles storage.

### Decision: Mongo CAS via `find_one_and_update`; `transaction()` best-effort

**Chosen:** `MongoMetadataStore.put_version` implements CAS with
`find_one_and_update({_id, version_number: expected}, ...)`, raising `ConflictError` when
no document matches — mirroring the SQL `WHERE version_number = ?` semantics.
`transaction()` provides true atomicity on SQL backends and on Mongo replica-set
deployments; on standalone Mongo it is a documented best-effort no-op.

**Rationale:** Single-document CAS is the only atomicity the version write path requires.
Multi-document atomicity is only needed by `move()`, addressed by the ordering decision below rather than by forcing a replica-set deployment on every Mongo user.

### Decision: `move()` writes destination-before-source for non-destructive partial failure

**Chosen:** Reorder `VFS.move()` so the destination version is created **before** the source is tombstoned (today it tombstones source first — [vfs.py:417-442](../../../src/vfs/vfs.py#L417)).
Keep the `self._meta.transaction()` wrapper: on SQL and Mongo-replica-set deployments the move is fully atomic; on standalone Mongo (no-op transaction) a crash between the two writes leaves the file present at both source and destination — a harmless duplicate the caller can re-resolve — never a vanished file.

**Rationale:** The reviewer correctly flagged that a no-op `transaction()` makes `move()` non-atomic on Mongo and that the original write order risks a tombstoned source with no destination (data loss).
Reordering converts the worst-case failure from _loss_ to _duplication_, which is recoverable, without imposing a replica-set requirement.

**Alternatives considered:**

- Force Mongo replica sets for real transactions: imposes deployment constraints on all
  Mongo users; single-node dev still needs the no-op path.
- Compensation/saga on failure: more moving parts than reordering for a two-write op.

### Decision: tier evaluation lives in `GarbageCollector`; adapters expose `iter_versions_for_gc`

**Chosen:** Add `iter_versions_for_gc(namespace_id, file_path) -> AsyncIterator[VersionMeta]` to the `MetadataStore` port — a coarse enumerator returning a file's versions in deterministic order (`created_at`, then `version_number`).
The tier-window math lives once in `GarbageCollector`: walk a file's versions, assign each to its age-band tier, and within each `keep_every` window keep the smallest-`created_at` version.
`list_reclaimable_versions` remains for the Phase 1 simple rules; tier-aware reclamation calls the library evaluator over `iter_versions_for_gc`.

**Rationale:** Time-window arithmetic in SQL and Mongo would be divergent implementations that must produce byte-identical reclamation sets — a correctness and test burden.
A single client-side evaluator over a coarse enumerator keeps adapters agnostic and guarantees identical decisions across backends (the cross-adapter test asserts this).

**Alternatives considered:**

- Per-adapter tier SQL/aggregation: divergent implementations, hard to keep identical,
  pushes time semantics into stores that should stay dumb.

### Decision: no observability delta

**Chosen:** Do not add an `observability` delta spec.
New adapters reuse the existing `metadata.*`/`blob.*` sub-operation spans under the baseline `OTelSpansOnAllOperations`; no new observability _contract_ is introduced.

**Rationale:** Phase 2 storage adds new emission sites for existing spans/metrics, which is
implementation under the current baseline.

## Architecture

ai-vfs follows a **ports-and-adapters** structure.
The protocols — `MetadataStore`, `BlobStore`, `SearchProvider`, `ExecutionProvider` — are the _ports_: consumer-defined contracts describing what the VFS needs, not how it is done.
Concrete backends are _adapters_ that conform to a port structurally (Python `typing.Protocol`, no inheritance required).
The `VFS` is the single _consumer_: it depends only on the ports, never on a named adapter.
Both the VFS and the adapters depend on the abstraction (dependency inversion), so new backends proliferate behind a port without changing the VFS or any sibling adapter.
This change adds adapters and extends two ports; it adds no consumer-side coupling.

```text
                         VFS (single consumer: permissions, versioning, CAS, move ordering)
                          │  depends only on ports ▼
   ┌──────────────────────┴───────────────────────────────────────┐
   MetadataStore (port)                        BlobStore (port)
   ┌───────────┬───────────────┬──────────┐    ┌──────────────┬──────────────┐
   SQLite      Postgres        Mongo            LocalFS         S3
   (Core+      (Core+asyncpg;  (Motor;          {base}/ab/cd/   s3://b/aifs/ab/cd/<hash>
    aiosqlite)  JSONB)          find_one_and_     <hash>         (CachedBlobStore wraps
   shared Core schema +         update CAS;                      remote: auto-enable s3://)
   Alembic migrations           transaction()
   + iter_versions_for_gc       best-effort)
   (coarse enumerator)

   GC: GarbageCollector (library, single canonical impl)
     simple rules → list_reclaimable_versions(policy)
     tier rules   → iter_versions_for_gc(file) ──► client-side tier-window evaluator
                    (identical reclamation across SQLite / Postgres / Mongo)

   move(src,dst): create dst version FIRST, then tombstone src
     atomic on SQL + Mongo-replica-set; non-destructive (duplicate) on standalone Mongo
```

URI resolution selects adapters at construction: `sqlite:///`→SQLite, `postgresql://`→
Postgres, `mongodb://`→Mongo (metadata); `file:///`→LocalFS, `s3://`→S3 (blob, cache
auto-enabled).

## Risks

- **SQLite-on-Core regression**: re-expressing `SQLiteMetadataStore` on Core could change CAS or JSON behavior.
  _Mitigation:_ the Phase 1 storage tests must pass unchanged against the Core-based SQLite adapter before Postgres lands.
- **SQLite vs Postgres JSON dialect drift**: SQLite stores JSON as TEXT; Postgres uses true JSONB — operators and ordering differ.
  _Mitigation:_ confine JSON access to value get/set; cover both dialects in round-trip tests.
- **Optional-dependency import boundaries**: importing an adapter without its extra must raise a clear, actionable error and never be imported by the VFS core.
  _Mitigation:_ guard imports behind the resolver; unit-test the missing-extra error path.
- **Mongo non-atomic `move()` on standalone**: a crash mid-move leaves a duplicate.
  _Mitigation:_ the destination-first ordering makes this non-destructive; documented in the protocol; replica-set deployments get full atomicity.

## Verification Notes

All SHALL requirements are covered by runnable evidence — unit tests for CAS, move ordering, tier-window evaluation, and missing-extra import errors; integration tests against real Postgres/Mongo/MinIO via Docker Compose fixtures (`integration_lifecycle` where subprocess lifecycle applies).
The cross-adapter tier test exercises SQLite, Postgres, and Mongo against one fixed version set.
No Verification Waivers are required.
