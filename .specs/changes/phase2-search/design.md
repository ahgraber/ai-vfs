# Design: Phase 2 (Search) — Native Full-Text Search and the SearchArtifact Envelope

## Context

Phase 1 ships a single `DefaultSearchProvider` (glob/find/regex) with the protocol `search(query, scope, search_type, candidates, fetch_content) -> list[SearchResult]` and `index(...) -> dict` ([src/vfs/protocols/search.py](../../../src/vfs/protocols/search.py), [src/vfs/search/default.py](../../../src/vfs/search/default.py)).
Regex brute-forces: `fetch_content(path)` reads the _latest_ version by path ([vfs.py:584](../../../src/vfs/vfs.py#L584)).

This change adds index-accelerated regex/fulltext and the contracts it needs.
It supersedes the bloom direction (see the superseded `ai-vfs-bloom-provider-design.md`): bloom passes ~20–22% of the corpus as candidates and never beats `ripgrep` through 100k docs, while SQLite FTS5 answers in \<1 ms. The principle: **bloom reduces blob reads; full-text search avoids them on the hot path** — so verification runs against indexed text, not blobs.

The load-bearing architectural decision (below): native FTS is a **metadata-store capability**, not a search provider.
A "provider" that wrote FTS rows into the metadata DB would violate the Phase 1 invariant that providers never touch stores directly ([search.py:10](../../../src/vfs/protocols/search.py#L10)); and the index's schema/migrations/transactions/GC/at-rest-confidentiality are storage-lifecycle concerns.
So the store owns the index machinery and the search layer owns orchestration.

Decisions are ordered by build dependency, matching `proposal.md` Scope and `tasks.md`.

## Decisions

### Decision: SearchArtifact envelope with provider-owned payload or external reference

**Chosen:** Replace the opaque `search_meta` dict with a manifest mapping `provider_key -> SearchArtifact`.
The envelope carries `status` (`ready`/`failed`/`unsupported`), `schema_version`, `provider_key`, `provider_version`, `params_hash`, `content_hash`, `created_at`, a `storage` discriminator (`inline`/`blob`/`external`), `error_code`/`error_message`, and either an inline `payload` or an `artifact_ref`.
For native FTS the artifact is `external`, its `artifact_ref` naming the content-addressed text record.

**Rationale:** The VFS reasons over freshness/lifecycle generically (`params_hash` and `content_hash` drive staleness; `status` drives usability) while payload/record schema stays private.
Usability requires `status==ready`, `content_hash` match, `params_hash` match, and — for `external` — that the referenced record is readable and identity-matched; otherwise the file is treated as an unindexed straggler, never a confirmed non-match.

### Decision: `search()` takes `SearchRequest`; `index()` returns `SearchArtifact | None`

**Chosen:** Migrate to `search(request: SearchRequest) -> SearchResponse`, where `SearchRequest` bundles `query`, `scope`, `search_type`, permission-pruned `search_metas`, a guarded `read_content`, `SearchLimits` (just `max_content_reads` now), and `find_predicates`.
`index()` returns `SearchArtifact | None`.
The Phase 1 `DefaultSearchProvider` migrates in this change.

**Rationale:** A bundled request keeps VFS dispatch generic and lets the orchestration layer own the pipeline.
Breaking change, acceptable pre-1.0 with one in-tree implementor.

### Decision: the content reader is a guarded object, content-hash-keyed, straggler-only

**Chosen:** `read_content` is not a bare callable.
The VFS constructs a guarded reader per request, bound to the enumerated `SearchMetaEntry` set.
It resolves a path to **that entry's `content_hash`** and fetches the blob by hash (never "latest by path"), enforces `max_content_reads` as a hard ceiling (raising `ReadBudgetExceeded`), and refuses out-of-scope paths.
It is used **only** for the bounded straggler-verification path — fresh native-FTS matches never touch it.

**Rationale:** Reading by `content_hash` (immutable) eliminates the race where a concurrent write changes "latest" between enumeration and verification, and matches the artifact.
The hard ceiling is what makes the cold-index path fail loud (below) rather than storm the blob store.

### Decision: native FTS is a metadata-store capability, not a search provider

**Chosen:** Define an optional `NativeTextSearch` capability the metadata store _may_ expose
(`meta.native_text_search()` returns it or `None`):

```python
class NativeTextSearch(Protocol):
    async def index_text(self, version_id, content_hash, params_hash, text) -> SearchArtifact: ...
    async def search_text(self, request, visible_version_ids) -> SearchResponse: ...
    async def delete_text_artifacts(self, content_hashes, retired_params_hashes) -> None: ...
```

SQLite (FTS5) and Postgres (`tsvector` + `pg_trgm`) implement it in `phase2-storage`; the implementations own their tables, migrations, transactions, and GC.
The capability is **not** on the base `MetadataStore` protocol — accessed via `native_text_search()` so adapters without it (Mongo) implement no stubs.

**Rationale:** Resolves the boundary tension: the store owns the DB, so the index lives where its lifecycle already is, and no "provider" needs a DB handle.
This collapses the per-backend `SqliteFtsProvider`/`PostgresFtsProvider` classes into one store capability with two implementations, and puts GC, migrations, and at-rest confidentiality on the storage side where they belong.

**Alternatives considered:**

- Per-backend `SearchProvider` classes reaching into the metadata DB: violates "providers don't
  touch stores"; no clean ownership of migrations/GC.
- `fts_candidates() -> version_ids` + verify via guarded reader: degenerates FTS into a better
  bloom filter — still reads candidate blobs, defeating the pivot's whole premise.
- A separate `SearchIndexStore` port: warranted only for a non-DB external engine (semantic,
  Elasticsearch) — deferred until one exists.

### Decision: content is the searchable document; a version is an occurrence

**Chosen:** Key the searchable text by `(provider_key, params_hash, content_hash)`, mirroring how
blobs key bytes by `content_hash`:

```text
content_hash                                → bytes                  (blob: document content)
(provider_key, params_hash, content_hash)   → decoded text + status  (search: document text)
version_id                                   → path, version_number, content_hash  (occurrence)
```

`search_text` matches **content**, then expands matches through the `visible_version_ids` that reference that content (the store joins its own version table), emitting path/version of each occurrence.
Result identity always comes from the VFS-enumerated visible version, never the text record's stored fields.
Rollback that reuses a `content_hash` is a free `search_meta` copy — the copied `external` reference still resolves because the record was never version-keyed.

**Rationale:** Content-addressing fixes the rollback-aliasing risk and gives dedup for free (same
content indexed once). `params_hash` in the key lets a tokenizer/extractor change produce a new
record without clobbering the old, and lets a profile be retired by sweeping its `params_hash`.

### Decision: verify against stored _raw_ text; mechanism differs per backend

**Chosen:** `index_text` persists the **exact decoded content as text** (the verification substrate), with the normalized tokens/`tsvector` as a _derived_ index over it; `params_hash` covers the derived index's config, not the raw text.
Regex/fulltext then verify against the stored raw text:

- **Postgres:** `WHERE text ~ :pattern` with a `gin_trgm_ops` index — the engine evaluates the real regex against the raw column, trigram-pruned.
  In-engine, no blob read.
- **SQLite:** FTS5 trigram prunes; exact regex is verified in-process (`re` over the pruned rows' raw text, via `content=`-backed FTS5 or a `REGEXP` UDF).
  Text comes from the DB, not S3.

Fulltext (ranked) uses `bm25()` (SQLite) / `ts_rank` (Postgres) directly.

**Rationale:** Normalized index text would give wrong regex answers; exact verification needs the raw bytes-as-text.
The benefit (no blob read) holds either way — the difference is in-engine (Postgres) vs in-process over DB-resident text (SQLite).

### Decision: dispatch by capability; glob/find floor; Mongo regex/fulltext deferred

**Chosen:** The VFS resolves visible current versions, then dispatches: if the metadata store exposes `NativeTextSearch` for `regex`/`fulltext`, use `search_text`; otherwise serve glob/find via `DefaultSearchProvider`.
A store without the capability (**MongoDB**) has no accelerated regex/fulltext and — because whole-scope brute force is deferred — no fallback for them this phase. glob/find (metadata-only) work on every backend.

**Rationale:** Keeps one dispatch rule and a backend-independent metadata-only floor, while
accelerated content search rides the store that can do it without blob reads.

### Decision: cold or unavailable index fails loud; bounded straggler verification

**Chosen:** Amend the Phase 1-era "never fail, only degrade" principle:

- **Fresh index** (artifact `ready`, `content_hash`/`params_hash` current, record present) →
  complete results, **zero blob reads**.
- **A bounded set of stragglers** (individual unindexed/stale/`unsupported`/missing-record files,
  ≤ `max_content_reads`) → verified individually via the guarded reader; the VFS MAY backfill.
- **A cold or unavailable index** (the index store errors, or stragglers exceed `max_content_reads`) → **fail loud** with an actionable error (`IndexUnavailableError` / `ReindexRequiredError`).
  Never a silent partial result, never an unbounded blob-read storm.

No-false-negatives holds on the fresh path (verification is the arbiter for what is searched); the cold path fails rather than risk a false negative or a storm.
Content-level index errors (undecodable, oversized) during `index_text` write a `failed`/`unsupported` artifact in the same transaction (the write succeeds); infrastructure errors abort the write transaction.

**Rationale:** Removes the silent-partial / outage-amplifier contradiction.
A healthy index is fast and complete; a broken one says "reindex" loudly.
The bounded-partial mode the reviewer floated is deferred, not built.

**Alternatives considered:**

- "Always degrade to brute force": over a large cold corpus this is a blob-read storm — deferred
  with the rest of brute-force scope management.

### Decision: reindex, bounded lazy backfill, rollback copy

**Chosen:** `vfs.reindex(namespace, provider, scope)` batch-backfills.
A provider/capability never self-triggers backfill; after the VFS reads content to verify a _bounded_ straggler set, it MAY call `index_text` and persist the artifact under the write-time CAS check.
Rollback reusing a `content_hash` copies `search_meta` (free — content-addressed).
GC deletes text artifacts by derivation (no eager refcount): when a `content_hash` has no retained version references (the same orphan check blob GC uses) and for retired `params_hash` profiles.

**Rationale:** Keeps backfill VFS-owned and bounded (consistent with cold-fails-loud), and reuses
the proven blob-GC orphan-derivation rather than fragile eager counts.

### Decision: the NativeTextSearch implementation lives on the store classes, introduced by this change

**Chosen:** The SQLite and Postgres `NativeTextSearch` implementations are methods on the `SQLiteMetadataStore`/`PostgresMetadataStore` classes — the store owns the `search_text_artifacts` table, in-transaction `index_text`, the derived index (SQLite FTS5; Postgres `tsvector` + `pg_trgm`), and GC.
But this _change_ introduces them: `phase2-search` adds the table, migration, methods, and the GC fold onto the store classes that `phase2-storage` builds.
`phase2-storage` contains no search code; the dependency is one-directional (`phase2-storage` → `phase2-search`).

**Rationale:** Code location and lifecycle ownership (the store owns the index machinery) are separate from change ownership (the feature that needs it introduces it).
Keeping all FTS in `phase2-search` breaks the dependency cycle that arises if `phase2-storage` references the search-defined protocol/envelope, and preserves `phase2-storage` as an independently-applicable change.

**Alternatives considered:**

- Implement `NativeTextSearch` in `phase2-storage`: forces it to depend on `phase2-search` for the
  protocol/envelope while `phase2-search` depends on it for the store classes — a cycle; neither
  could be applied first.
- Extract a third `phase2-search-foundation` change (envelope + protocol) both depend on: a clean
  DAG but over-split; fragments the search work.

**Confidentiality (classification change):** storing decoded text makes the metadata DB **content-bearing**, at the same sensitivity as the blob store — not merely metadata-bearing.
It MUST be protected accordingly: encryption at rest, least-privilege DB roles, restricted backups/replicas/analytics access.
VFS RBAC is query-time only and does not cover operator or at-rest access.
Text artifacts are content-addressed and therefore shared across namespaces at rest (like blobs); namespace isolation holds at the query boundary, not in the physical row.
GC MUST delete text artifacts when their content is reclaimed (retention/erasure compliance).

## Architecture

ai-vfs follows **ports-and-adapters**.
`SearchProvider` is a port (glob/find/regex floor); `NativeTextSearch` is an optional capability of the `MetadataStore` port.
The `VFS` is the single consumer: it dispatches to the store capability when present, else the glob/find floor — never knowing a concrete index by name.

```text
   write(path, bytes) ─► VFS ─► content_hash = blake3(content)
                          │  blob.put(content_hash, bytes)   [idempotent, before txn]
                          │  if store.native_text_search():  index_text(version_id, content_hash,
                          │      params_hash, raw_text)  ── in the version's DB transaction ──►
                          │      upsert text artifact keyed by (provider_key, params_hash, content_hash)
                          │      return external SearchArtifact → search_meta[provider_key]

   search(query, scope, type) ─► VFS: authorize ─► resolve scope ─► permission-prune ─► visible versions
                          │
            ┌─────────────┴───────────────────────────────────────────────┐
   store.native_text_search() for regex/fulltext?              else (glob/find, or Mongo regex/fulltext)
            │ yes                                                          │
            ▼                                                              ▼
   nts.search_text(request, visible_version_ids)            DefaultSearchProvider (glob/find = metadata;
     match content (verify raw text, NO blob read)           regex on Mongo = deferred this phase)
     → expand to visible occurrences (join version table)
     → SearchResponse(results: path/version)
            │
            │ stragglers (≤ max_content_reads): guarded reader verifies by content_hash
            │ cold/unavailable index OR stragglers > budget: FAIL LOUD (reindex/unavailable)
            ▼
   guarded read_content: path → entry.content_hash (immutable), hard max_content_reads ceiling,
     out-of-scope refused — straggler path only; fresh matches never read blobs

   identity:  content_hash → text artifact (shared, content-addressed)
              version_id   → occurrence (path/version); results carry occurrence identity
   GC:        delete text artifacts when content_hash orphaned (blob-GC orphan check) +
              retired params_hash sweep — derived, no eager refcount
```

The `NativeTextSearch` _protocol_ is defined in this change; its SQLite/Postgres _implementations_
(tables, migrations, GC, plaintext-at-rest handling) land in `phase2-storage` — so this change has
a build dependency on `phase2-storage`.

## Risks

- **Per-backend verification divergence**: Postgres verifies regex in-engine; SQLite in-process.
  _Mitigation:_ a shared contract test asserts identical matching-path sets for the same query across SQLite, Postgres, and the brute-force baseline.
- **Plaintext at rest (confidentiality)**: storing decoded text makes the metadata DB content-bearing, at the blob store's sensitivity tier.
  _Mitigation:_ see the classification-change note in the implementation decision above — encryption at rest, least-privilege, restricted backups/replicas; GC deletes text on content orphan.
  VFS RBAC is query-time only and does not cover operator/at-rest access.
- **Cold-index fail-loud UX**: an outage turns searches into errors, not slow successes.
  _Mitigation:_ the error is actionable (reindex/unavailable); glob/find still work; this is the conscious trade for deferring bounded-partial mode.
- **Raw-text storage cost**: full decoded text per content hash (≈ corpus size).
  _Mitigation:_ content-addressed (stored once per content), GC'd with blob orphans.
- **Breaking protocol change**: `search()`/`index()` signatures change.
  _Mitigation:_ one in-tree implementor migrated here; no third-party providers exist pre-1.0.

## Verification Notes

All SHALL requirements are covered by runnable evidence — unit tests for the envelope (incl. external-record usability and full field set), the guarded reader (content-hash binding, budget ceiling, out-of-scope refusal), document/occurrence expansion, rollback content-addressed copy, and cold-index fail-loud; integration tests for the SQLite and Postgres `NativeTextSearch` implementations (the latter against the Postgres fixture), plus a contract test asserting result-set equivalence across both and the brute-force baseline.
No Verification Waivers required.
