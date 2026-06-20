# Object-Store Text Index — Design

> Change: `object-store-text-index`
> Date: 2026-06-11

## Context

`NativeTextSearch` accelerates regex/fulltext by verifying against stored text instead of reading blobs ([protocols/search.py:84](../../../src/vfs/protocols/search.py#L84)).
Today it is reachable **only** through the metadata store: the VFS calls `self._meta.native_text_search()` on write ([vfs.py:333](../../../src/vfs/vfs.py#L333)), search ([vfs.py:888](../../../src/vfs/vfs.py#L888)), and reindex ([vfs.py:1243](../../../src/vfs/vfs.py#L1243)), and `index_text` runs inside `self._meta.transaction()` ([vfs.py:1254](../../../src/vfs/vfs.py#L1254)), returning a `ready` artifact immediately.
Three metadata-store guarantees follow that do **not** hold for an object-store index: indexing is atomic with the version write, a committed version is immediately searchable, and dispatch can assume one capability serves both regex and fulltext.

This change makes the capability provisionable independently of the metadata store and evolves the protocol to model the differences explicitly.
It is the SmithDB approach (inverted index in object storage) reduced to the minimum that beats brute-force, plus the corrections from critical review.

## Decisions

### Decision: Protocol evolution — capability declaration + indexing discipline (review #2, #3)

**Choice:** Evolve `NativeTextSearch` additively rather than introduce a parallel interface.
Add:

- `supported_search_types: set[SearchType]` — the VFS routes a search type to the capability only if declared.
  SQLite/Postgres declare `{REGEX, FULLTEXT}`; the object-store index declares `{FULLTEXT}`.
- An **indexing discipline**: _synchronous_ (today's in-transaction `index_text`, returns `ready`) or
  _deferred_ (`index_text` stages; a new `materialize(namespace_id, params_hash)` seals and backfills;
  no-op for synchronous capabilities).

**Rationale:** The earlier draft's "protocol unchanged" claim was wrong — staging-then-sealing has no home in a protocol whose `index_text` must return a `ready` artifact in the write transaction, and "any native capability serves both types" contradicts a FULLTEXT-only index.
Both are additive: the two implementations still share `search_text`, `delete_text_artifacts`, and the document/occurrence model.
A `supported_search_types` declaration replaces the mechanical `self._meta`→`self._nts` swap that the review correctly called insufficient.

**Alternative:** a distinct `DeferredTextIndex` protocol.
Rejected — it would duplicate `search_text`, identity, and GC semantics for no gain; the differences are two declarations and one method.

### Decision: Consistency — manually-materialized, not eventually-consistent (review #7)

**Choice:** Indexing is **deferred and manually materialized**.
A write stages content; only `reindex` (calling `materialize`) seals it.
Unsealed versions are stragglers covered by `max_content_reads`; beyond the budget, FULLTEXT **fails loud** (reindex-required).

**Rationale:** The review is correct that reindex-only is _not_ eventual consistency — without an automatic builder, unsealed content never self-converges, so after >`max_content_reads` fresh writes a broad FULLTEXT fails until an operator reindexes.
Calling that "eventual" was wrong.
It is an honest, documented MVP contract for a capability that is **off by default** and serves backends with _no_ fulltext otherwise; the failure is loud, never silent partial results.
A background builder is the **liveness** mechanism that upgrades "manual" → "automatic"; it is deferred, and the operational requirement (run reindex after bulk writes) is documented.

**Alternatives:** _Synchronous per-version index object on write_ — no transaction to join (non-atomic, risks orphaned index objects) and a GET-per-file query pattern SmithDB calls catastrophic; rejected.
_In-scope background builder_ — true liveness, but a background process is its own design/test surface; deferred to keep the MVP minimal.

### Decision: Manifest in the metadata store under CAS (review #1, #4)

**Choice:** The mutable per-`(namespace_id, params_hash)` manifest (the list of live segment hashes) lives in the **metadata store** under compare-and-swap; postings live in the `BlobStore` as immutable segments.
Publication order: **write segment → CAS manifest → backfill artifacts**.

**Rationale:** The content-addressed `BlobStore` cannot host a mutable manifest — `put()` is hash-keyed and idempotent-no-ops on an existing key ([local_blob.py:12](../../../src/vfs/stores/local_blob.py#L12), [s3_blob.py:130](../../../src/vfs/stores/s3_blob.py#L130)), and the cache assumes immutable values.
The metadata store already provides CAS (`MetadataCASSemantics`) on all three backends (SQLite/Postgres via `WHERE version=?`, Mongo via `find_one_and_update`).
This resolves the review's last-writer-wins hazard: the manifest update is an atomic CAS, so a racing publisher's update never blindly overwrites another's — the loser retries against the current manifest and both segments stay live.
The expensive postings stay object-store-resident; only a tiny pointer list touches metadata, so the decoupling thesis holds.

**Alternative:** a named-object `BlobStore` interface (overwrite + ETag CAS + prefix-enum + cache invalidation across local/S3/cached).
More faithful to "fully object-resident," but real new surface on three backends re-implementing CAS the metadata store already has.
Rejected for the MVP.

### Decision: Stable logical artifact identity (review #5)

**Choice:** `artifact_ref` names the **logical** `(provider_key, params_hash, content_hash)` text identity, never a segment hash.
Liveness = the content_hash is present in some live segment per the manifest.
An absent content_hash (not yet sealed, or compacted mid-flight) resolves as a straggler.

**Rationale:** If `artifact_ref` named a physical segment, compaction (which rewrites segments) would invalidate every version artifact pointing at the old segment.
A logical reference is compaction-stable and reuses the existing `ExternalRecordMissingOrMismatchedIsStale` semantics verbatim — a missing record is already defined as "straggler, never confirmed non-match." `has_text` / resolution is a content→ segment lookup over the manifest's live segments.

### Decision: Whole-segment query I/O with bounded segment size (review #6)

**Choice:** `BlobStore` is whole-object only, so the MVP reads **whole** segments.
Each segment is byte-budgeted (default target ~8 MB compressed; configurable).
A query reads the manifest, then reads the live segments whole, scanning each segment's term dictionary for the query terms.

**Rationale:** "Per-term postings fetch" implied range reads the `BlobStore` does not offer.
Bounding segment size keeps whole-segment reads bounded — the same byte-budget instinct as SmithDB's row groups, without its FST/range-read machinery.
At ai-vfs's target scale (tens of thousands of files/namespace, per the bloom benchmark) a namespace partition is a handful of segments; reading them whole per query is acceptable.
Range reads, per-term postings objects, and FST term dictionaries are the named upgrade path if latency proves inadequate.
Compaction keeps the live-segment count low (and thus query cost bounded) by merging small segments.

### Decision: BM25, fully parameterized and folded into `params_hash` (review #8)

**Choice:** Score with Okapi BM25:

```text
score(q, d) = Σ_{t∈q} IDF(t) · ( f(t,d)·(k1+1) ) / ( f(t,d) + k1·(1 − b + b·|d|/avgdl) )
IDF(t)      = ln( 1 + (N − n(t) + 0.5) / (n(t) + 0.5) )
```

with fixed defaults **k1 = 1.2, b = 0.75**.
Definitions and scope, all pinned into `params_hash`:

- **|d| (document length)** = token count of the content, stored per `content_hash` in the segment.
- **f(t,d) (term frequency)** = occurrences of term `t` in document `d`, stored per posting.
- **N (corpus size)** and **avgdl (average doc length)** are computed over the **distinct live
  content hashes** of the `(namespace_id, params_hash)` partition (corpus = the namespace partition),
  derived from the manifest's live segments at query time — so they are deterministic given a manifest.
- **Cross-segment dedup**: a `content_hash` is indexed once across live segments; if compaction
  transiently leaves a duplicate, query-time dedup by `content_hash` keeps `N`/DF correct.
- **Tokenizer/normalization**: Unicode-aware word tokenization + lowercase fold (no stemming, no stopword removal in the MVP).
  The exact ruleset is part of `params_hash`, so changing it produces a new profile and a retired-profile sweep rather than silently corrupting scores.
- **Stale documents**: only versions whose `content_hash` is live in the manifest contribute to
  `N`/avgdl/DF; orphaned content is removed by the GC erasure sweep, so it cannot skew the corpus.
- **Straggler ranking**: straggler matches (unsealed content verified via the guarded reader) are appended **after** the BM25-ranked sealed results with a sentinel score, matching the current VFS behavior of appending verified stragglers rather than globally re-ranking.
  This is documented as an approximation: a straggler's score is not globally comparable because its DF is not in the manifest.
  Stragglers are bounded by `max_content_reads`, so the unranked tail is small by construction.

**Rationale:** `RankedFulltext` is a hard requirement (SmithDB ships no ranking; ai-vfs must).
Pinning every parameter into `params_hash` makes ranking reproducible and lets a tuning change retire the old profile cleanly.
Positions are omitted (no phrase search), so only TF/DF/length are needed.

### Decision: Mandatory erasure on GC (review #9)

**Choice:** The index sweep leaves **no live segment indexing orphaned content**.
A fully-orphaned segment is dropped; a partially-orphaned segment is **compacted in the same sweep** — rewritten without the orphaned postings and CAS-republished.
Compaction is therefore GC-triggered, not optional.

**Rationale:** The review correctly flagged that "optional compaction" lets deleted content's text persist in a mixed segment indefinitely — a privacy failure.
Making erasure mandatory on GC mirrors the content-artifact GC guarantee (`TextArtifactGcFollowsContentOrphan`) at the segment level.
An erasure test asserts that after deleting content and running GC, the term no longer resolves in any live segment.

### Decision: Provisioning — store-FTS wins, object index only when store lacks FTS

**Choice:** Resolve one active capability at construction: `self._meta.native_text_search()` if present, else a configured `ObjectStoreTextIndex`, else none.
Store-FTS wins; never double-index.

**Rationale:** SQLite FTS5 is sub-millisecond (bloom benchmark) and beats object-store round trips.
Double-indexing wastes writes and storage.
The object index is purely the gap-filler for non-FTS backends.

## Architecture

```text
                         VFS construction
   metadata_store_uri ──► MetadataStore ──► native_text_search() ─┐
                                                                  ├─► resolve ONE active NativeTextSearch
   text_index_*  ───────► ObjectStoreTextIndex(BlobStore, Meta) ──┘   (store-FTS wins; declares types)
                                   │
   ┌───────────────────────────────┼───────────────────────────────────────────────┐
   │ write(path, content)          │ search(type)                  │ reindex(scope)  │
   ▼                               ▼                               ▼                 │
 put blob                  type ∈ supported_search_types?     materialize:           │
 discipline == deferred?   ├─ no  → brute-force / unsupported  seal staged content    │
   stage text (NO seal)    └─ yes → read manifest (metadata)    → immutable segment    │
   (version = straggler)          read live segments WHOLE      → CAS manifest (meta)  │
                                   BM25 rank (N,avgdl from man.) → backfill artifacts   │
                                   expand → visible occurrences                        │
                                   unsealed tail → guarded reader (≤ max_content_reads)│
                                   over budget → FAIL LOUD (reindex-required)          │
   └───────────────────────────────────────────────────────────────────────────────┘

   Split substrate:
     postings   → BlobStore   : <prefix>/{seghash[0:2]}/{seghash[2:4]}/{seghash}  (immutable, size-bounded)
     manifest   → MetadataStore: row/doc keyed (namespace_id, params_hash), CAS-updated  (mutable)
     artifact   → version.search_meta[provider_key] : logical ref (provider_key, params_hash, content_hash)

   Publish order (materialize & compaction): write segment → CAS manifest → backfill artifacts
   GC erasure: fully-orphaned segment dropped; mixed segment compacted (rewrite w/o orphaned postings)
```

## Risks

- **Manual-materialization liveness gap.**
  Broad FULLTEXT fails loud once >`max_content_reads` versions are unsealed.
  _Mitigation:_ intended contract; document "run reindex after bulk writes"; the deferred background builder closes it.
  The failure is loud (reindex-required), never a silent miss.
- **Stale manifest read mid-publish.**
  A query reading the manifest concurrently with a republish may miss a just-sealed segment.
  _Mitigation:_ missing content degrades to a straggler (verified within budget), never a false negative; manifest CAS makes republish atomic, and reindex is idempotent.
- **Whole-segment read cost growth.**
  Many small segments inflate per-query bytes.
  _Mitigation:_ size-bounded segments + compaction merging small segments keep the live-segment count and total bytes bounded; range reads are the deferred scale path.
- **Confidentiality.**
  Index text/postings carry blob-level content.
  _Mitigation:_ spec requires the substrate be treated at blob confidentiality; mandatory GC erasure removes orphaned content's postings on the next sweep.

## Verification Waivers

None.
Every SHALL has runnable evidence against a local `BlobStore` + a metadata store stubbed to return `None` from `native_text_search()` (or MongoDB): capability declaration + dispatch, provisioning resolution, deferred staging vs. synchronous indexing, materialize seal + CAS publish ordering, whole-segment BM25 ranking, sealed-content-no-blob-reads, unsealed-straggler, fail-loud, occurrence expansion, logical-ref-survives-compaction, concurrent-publish-no-lost-segment, and mandatory GC erasure (delete-then-GC-then-term-absent).
