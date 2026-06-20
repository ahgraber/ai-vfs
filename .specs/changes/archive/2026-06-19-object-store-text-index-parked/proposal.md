# Object-Store Text Index: Full-Text Search Decoupled from the Metadata Store

> **PARKED — deferred as scope creep (2026-06-19).** Not implemented and not part of the
> PoC. Object-store-resident postings are a nice-to-have for non-FTS metadata backends, but
> the PoC's search correctness floor (fail-loud + `reindex`) covers the gap. Resume by
> moving this directory back under `.specs/changes/`. Parked in `archive/` only to keep the
> active change set free of uncommitted scope.

**Change name:** `object-store-text-index` **Date:** 2026-06-11 **Author:** ahgraber + Claude

## Intent

Give FULLTEXT search to deployments whose **metadata store has no native full-text capability** — today only SQLite (FTS5) and Postgres (`tsvector` + `pg_trgm`) expose `NativeTextSearch`; MongoDB (and any future non-SQL metadata backend) returns `None`.
On those backends FULLTEXT raises `SearchTypeUnsupportedError` outright, and REGEX has only the bounded brute-force fallback.
There is no ranked full-text path at all.

This change adds an **object-store-resident inverted index** — a `NativeTextSearch` implementation whose postings live in the configured `BlobStore` (local FS, S3, or cached), independent of which metadata store is in use.
It is the reference design from [SmithDB's full-text search writeup](https://www.langchain.com/blog/full-text-search-in-smithdb-designing-an-inverted-index-for-object-storage), reduced to the minimum that beats brute-force: a term→postings inverted index with BM25 ranking, no FST/Vortex/block-bitpacking machinery.

**Why this is the right gap to fill.**
FULLTEXT is the capability genuinely _missing_ on non-FTS backends — REGEX already degrades safely to bounded brute-force everywhere.
Accelerating REGEX via the same index is a coherent follow-on but is **not** in this change.

**The architectural evolution.**
The phase2-search principle was _"native FTS is a storage capability, not a search provider"_ — the metadata store owns the index.
This change relaxes that to _"native FTS is a capability that MAY be backed by the object store instead of the metadata store."_
It requires an **explicit, additive evolution of the `NativeTextSearch` protocol** (not a no-op reuse, as an earlier draft wrongly assumed): the protocol gains a **capability declaration** (`supported_search_types`) and a **deferred-indexing discipline** (stage-then-seal) alongside the existing synchronous one.

**Two metadata-store assumptions break — and how each is resolved.**

1. **In-transaction indexing.**
   Today `index_text` runs inside `self._meta.transaction()` ([vfs.py:1254](../../../src/vfs/vfs.py#L1254)) and returns a `ready` artifact immediately.
   An object-store write cannot join a metadata-DB transaction.
   _Resolution:_ the protocol declares an indexing **discipline**.
   Synchronous capabilities (SQLite/Postgres) keep today's path.
   The object-store index is **deferred**: a write stages content but seals nothing; a `materialize` pass (driven by `reindex`) seals batches into immutable segments and backfills artifacts.
2. **Read-after-write freshness.**
   A just-written, not-yet-sealed version is a _straggler_ — verified individually via the guarded reader within `max_content_reads` (`BoundedStragglersVerified`), and **fails loud** (reindex-required) when the unsealed set exceeds the budget (`ColdIndexFailsLoud`).
   This reuses existing machinery — but note the consequence below.

**This is a _manually-materialized_ index, not an eventually-consistent one.**
Without an automatic builder, unsealed content does **not** self-converge: once more than `max_content_reads` files have been written since the last seal, broad FULLTEXT **fails loud until an operator runs `reindex`**.
That is an acceptable, honestly-stated MVP contract for a capability that is **off by default** and serves backends which otherwise have _no_ fulltext at all.
A background builder that makes the index self-converging is an explicit deferred follow-on (it is a _liveness_ mechanism, not a latency tweak).

**Where the index data lives.**
The expensive part — postings — lives in the `BlobStore` as **immutable, size-bounded, content-addressed segments**.
The one mutable structure, the per-`(namespace, params_hash)` **manifest** naming live segments, lives in the **metadata store**, which already provides compare-and-swap (`MetadataCASSemantics`, on SQLite/Postgres/Mongo).
The content-addressed `BlobStore` cannot host the manifest: `put()` is hash-keyed and idempotent-no-ops on an existing key ([local_blob.py:12](../../../src/vfs/stores/local_blob.py#L12)), and the cache assumes values are immutable.
Keeping the manifest in CAS-backed metadata also gives correct concurrent publication for free.

## Scope

> Build-dependency order: storage substrate → search protocol/provider → versioning lifecycle.
> `design.md` and `tasks.md` follow this order.

### In Scope

- **Object-store index substrate** (storage, foundation): immutable, **size-bounded**, content-addressed segment objects in the `BlobStore` (postings), plus a mutable per-`(namespace_id, params_hash)` **manifest persisted in the metadata store under CAS**.
  Segments are excluded from blob-orphan GC and reclaimed by a dedicated index sweep that **mandatorily compacts** segments to erase orphaned content.
  Gated by config; off by default.
- **`NativeTextSearch` protocol evolution** (search, foundation): an additive **capability declaration** `supported_search_types` so the VFS dispatches each search type only to a capability that declares it; and a **deferred-indexing discipline** with a `materialize`/seal entrypoint alongside the existing synchronous `index_text`.
  SQLite/Postgres remain synchronous and declare `{REGEX, FULLTEXT}`.
- **`ObjectStoreTextIndex` provider** (search): a deferred `NativeTextSearch` declaring `{FULLTEXT}`.
  It stages decoded UTF-8 text keyed by `(provider_key, params_hash, content_hash)`, seals batches into segments on `materialize`, answers FULLTEXT by reading the manifest + the relevant **whole** segments (no range reads), ranks with **BM25** (fully parameterized, folded into `params_hash`), and expands each content match through the permission-pruned visible occurrences — identical result-identity contract to the existing providers.
  The per-version `external` artifact references a **stable logical** `(provider_key, params_hash, content_hash)` identity, never a segment hash, so compaction never invalidates artifacts.
- **Decoupled native-capability provisioning** (search): the VFS resolves one active `NativeTextSearch` from the metadata store _or_ an independently-configured object-store index.
  Store-FTS wins when both exist (sub-millisecond DB index beats object-store round trips), so a backend with native FTS is never double-indexed.
- **Reindex materializes the object-store index** (versioning): `reindex` seals staged content into segments, **CAS-publishes** the manifest (order: write segment → CAS manifest → backfill artifacts), and backfills `external` artifacts.
  GC reclaims/compacts segments mirroring the text-artifact orphan condition.

### Out of Scope (deferred)

- **Background builder / automatic materialization** — the _liveness_ mechanism that makes the index self-converging.
  The MVP is manually materialized via `reindex`; the operational requirement is documented.
- **Accelerated REGEX via the object-store index** — REGEX keeps its bounded brute-force fallback.
- **Range reads / per-term postings objects** — the MVP reads size-bounded segments whole.
  SmithDB's FST term dictionaries, Vortex columnar layout, block-bitpacked delta postings, and zone-map pruning are the named upgrade path if query latency proves inadequate.
- **Positions / phrase search** — postings carry document membership + term frequency only.
- **Replacing SQL FTS where present** — the object-store index never double-indexes a backend that
  already has native FTS.
- **Cross-namespace global index** — index is partitioned per namespace.
- **Semantic search** — separate provider, separate change.

## Approach

- **Index unit = content, not version**, mirroring the existing providers: one set of postings per `(params_hash, content_hash)`; a version is an occurrence.
  Dedup and rollback-copy semantics carry over unchanged.
  A `content_hash` is indexed once across all live segments (cross-segment dedup).
- **Immutable size-bounded segments + CAS manifest.**
  Staging records pending content; `materialize` seals a byte-budgeted batch into an immutable segment (content-addressed by its own bytes, reusing `BlobStore` sharding + idempotent put) and publishes it by CAS-updating the manifest in the metadata store.
  Immutability makes segments cache-safe; compaction is write-new-segment-then-CAS-republish.
- **Whole-segment query I/O.**
  A query reads the manifest, then reads the relevant **whole** segments (each segment carries its own term dictionary + postings + per-doc term frequencies).
  Segment size is bounded so whole-segment reads stay bounded; this is the SmithDB byte-budget instinct without its range-read machinery.
  No full-payload blob scan — the failure mode SmithDB calls "catastrophic".
- **Stable logical artifact identity.**
  `artifact_ref` is the logical `(provider_key, params_hash, content_hash)`; liveness is "the content is present in some live segment per the manifest."
  A content_hash absent from every live segment (not yet sealed, or compacted away mid-flight) resolves as a straggler — reusing `ExternalRecordMissingOrMismatchedIsStale` exactly.
- **Mandatory erasure.**
  The index GC sweep SHALL leave no live segment indexing orphaned content: a fully-orphaned segment is dropped; a partially-orphaned segment is compacted (rewritten without the orphaned postings) within the same sweep — a privacy guarantee, not a deferred optimization.

## Open Questions

None blocking.
The two prior forks are resolved: the manifest lives in the **metadata store under CAS**, and the MVP is **manually materialized** (background builder deferred).
`design.md` records the rejected alternatives (synchronous-on-write indexing; a named-object `BlobStore` interface; an in-scope background builder) with rationale.
