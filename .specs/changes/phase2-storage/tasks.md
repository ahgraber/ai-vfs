# Tasks: Phase 2 (Storage)

## URI Resolution & SQL Schema Foundation

- [ ] Add `postgresql://`, `mongodb://`, `s3://` scheme branches to the store resolver, each importing its adapter lazily and re-raising a clear "install extra X" error on `ModuleNotFoundError`
- [ ] Define the shared metadata schema with SQLAlchemy Core (`Table` definitions for files, versions, permissions, audit, names) usable by both `aiosqlite` and `asyncpg` dialects
- [ ] Add Alembic scaffolding (env, initial migration matching the current schema) targeting both SQLite and Postgres dialects
- [ ] Re-express `SQLiteMetadataStore` on the Core schema, preserving `WHERE version_number = ?` CAS and JSON value get/set
- [ ] Test: existing Phase 1 storage tests pass unchanged against the Core-based `SQLiteMetadataStore` (regression gate)
- [ ] Test: resolver returns the right adapter for each scheme, and raises a missing-extra error naming the dependency when the extra is absent (`URIBasedStoreResolution`, `MissingExtraRaises`)

## Postgres Adapter

- [ ] Implement `PostgresMetadataStore` on the shared Core schema with `asyncpg`, JSONB columns for `search_meta`/`detail`, and `BEGIN`/`COMMIT`/`ROLLBACK` `transaction()`
- [ ] Implement Postgres CAS via `UPDATE ... WHERE version_number = ?` raising `ConflictError` on zero rows
- [ ] Test (integration, Postgres fixture): file + version round-trip with non-empty `search_meta`/`detail` returns equal models, JSONB-stored (`MetadataStoreProtocol`/`PostgresAdapterRoundTrip`)
- [ ] Test (integration): `put_version(expected_version=3)` against version 5 raises `ConflictError` (`MetadataCASSemantics`/`CASConflictDetected`, SQL write-site)
- [ ] Test (integration): `transaction()` rolls back all writes on mid-transaction error (`MetadataTransactions`/`TransactionRollbackOnError`)

## Mongo Adapter

- [ ] Implement `MongoMetadataStore` with Motor, native subdocuments for `search_meta`/`detail`, and a documented best-effort no-op `transaction()` (real transaction when a session/replica set is available)
- [ ] Implement Mongo CAS via `find_one_and_update({_id, version_number: expected}, ...)` raising `ConflictError` when no document matches
- [ ] Test (integration, Mongo fixture): file + version round-trip with non-empty extensible fields returns equal models, subdocument-stored (`MetadataStoreProtocol`/`MongoAdapterRoundTrip`)
- [ ] Test (integration): `put_version(expected_version=3)` against version 5 raises `ConflictError` via `find_one_and_update` (`MetadataCASSemantics`/`MongoCASConflict`, NoSQL write-site)

## Move Ordering

- [ ] Reorder `VFS.move()` to create the destination version before tombstoning the source, keeping the `transaction()` wrapper
- [ ] Test: move on a transactional store leaves neither partial destination nor source tombstone after an injected mid-operation failure (`MoveFile`/`MoveAtomicOnTransactionalStore`)
- [ ] Test: move on a best-effort `transaction()` store, failing after destination create and before source tombstone, leaves the file readable at both paths — no loss (`MoveFile`/`MoveNonDestructiveOnBestEffortStore`)

## S3 Blob Adapter & Cache

- [ ] Implement `S3BlobStore` with `aiobotocore`: `put`/`get`/`delete`/`exists`/`list_hashes`, content-hash keys under `{prefix}/{hash[0:2]}/{hash[2:4]}/{hash}`, idempotent `put`, streaming methods raising `NotImplementedError`
- [ ] Auto-enable the `diskcache` wrapper for `s3://` and disable it for `file:///` when `blob_cache_enabled` is unset
- [ ] Test (integration, MinIO fixture): `put` then `get` returns equal bytes; duplicate `put` is a no-op (`BlobStoreProtocol`/`S3AdapterRoundTrip`)
- [ ] Test (integration): a stored blob's object key is `{prefix}/ab/cd/<hash>` (`BlobPrefixDirectoryStructure`/`S3KeyStructure`)
- [ ] Test: auto mode wraps `s3://` in the cache and leaves `file:///` unwrapped (`BlobCaching`/`RemoteAutoEnable`, `LocalAutoDisable`)

## Tier-Based Retention

- [ ] Add `iter_versions_for_gc(namespace_id, file_path)` to the `MetadataStore` protocol and implement it on SQLite, Postgres, and Mongo (deterministic order: `created_at`, then `version_number`)
- [ ] Implement the tier-window evaluator in `GarbageCollector`: newest-first tier banding, smallest-`created_at` survivor per `keep_every` window, always preserving first/current
- [ ] Wire tier-aware reclamation to consume `iter_versions_for_gc`, retaining `list_reclaimable_versions` for the simple rules
- [ ] Test: hourly tier keeps one version per hour window plus first/current (`TierBasedRetention`/`HourlyTierKeepsOnePerHour`)
- [ ] Test: cascading tiers sample by band (all/hourly/daily/weekly) over a 60-day span (`TierBasedRetention`/`TiersCascadeNewestFirst`)
- [ ] Test: within-window survivor is the smallest-`created_at` version regardless of enumeration order (`TierBasedRetention`/`FirstWithinWindowIsDeterministic`)
- [ ] Test (integration): identical reclaimed version-ID set across SQLite, Postgres, and Mongo for one fixed version set + policy (`TierBasedRetention`/`ReclamationIdenticalAcrossAdapters`)

## Packaging

- [ ] Add `sqlalchemy` and `alembic` to the **core** dependencies in `pyproject.toml` (the default SQLite profile runs on Core)
- [ ] Declare optional dependency extras in `pyproject.toml`: `postgres` (`asyncpg`), `mongo` (`motor`), `s3` (`aiobotocore`)
- [ ] Add Docker Compose fixtures for Postgres, MongoDB, and MinIO used by the integration tests
- [ ] Update `CHANGELOG.md` under Unreleased with the new adapters and the `move()` ordering change
