# Phase 2 (Search): Native Full-Text Search and the SearchArtifact Envelope

**Change name:** `phase2-search` **Date:** 2026-05-22 **Author:** ahgraber + Claude

## Intent

Give ai-vfs a fast, index-accelerated search path for agentic grep that does **not** read file content from the blob store on the hot path.
This is the **search half** of the former `phase2-adapters` change, and it supersedes the earlier bloom-filter direction.

**Why not bloom.**
Benchmarking in the `bloom-search` sibling repo ([docs/benchmark/analysis.md](../../../../bloom-search/docs/benchmark/analysis.md)) shows bloom prefiltering passes ~20–22% of the corpus as candidates regardless of N and never beats plain `ripgrep` through 100k documents, while build memory reaches 1.13 GB at 100k.
For S3-backed grep, "read 20% of objects to verify" is a poor primitive.
The same benchmark shows SQLite FTS5 serving queries in under 1 ms (BM25) and 0.12–6.6 ms (trigram) at every scale.
The framing: **bloom reduces blob reads; DB full-text search avoids them on the hot path.**
Bloom is not pursued.

**Why not chunking.**
Semantic-style chunking adds boundary-crossing queries, partial-update semantics, and stale-chunk cleanup. ai-vfs versions are immutable and append-only, so the simplest correct unit is **one searchable text artifact per content hash** — content is the searchable document, a file version is an _occurrence_ of that content at a path.
Chunking is deferred to a future semantic provider.

**Native FTS is a storage capability, not a search provider.**
A native index owns DB schema objects, migrations, transactions, GC, and at-rest confidentiality — all storage-lifecycle concerns.
So the metadata store optionally implements a `NativeTextSearch` capability; the VFS uses it when present and the search layer owns only orchestration (envelope, dispatch, verify contract, degradation).
This avoids the boundary violation of a "search provider" reaching into the metadata store's database.

**Prerequisite:** `phase1-core` and `shell-context` (archived).
**Depends on `phase2-storage`** — this change adds the `NativeTextSearch` implementations onto the SQLite/Postgres store classes (Core schema, Postgres adapter) that `phase2-storage` builds, so it applies after it.
The dependency is one-directional (`phase1` → `phase2-storage` → `phase2-search`); `phase2-storage` knows nothing of search.
The `DefaultSearchProvider` (glob/find) remains the backend-independent floor.

## Scope

> Build-dependency order; `design.md` and `tasks.md` follow it.

### In Scope

- **`SearchArtifact` envelope** (foundation): standard manifest mapping `provider_key -> SearchArtifact` carrying common lifecycle/freshness fields (`status`, `schema_version`, `provider_version`, `params_hash`, `content_hash`, `created_at`, `storage`, error fields) with a provider-owned `payload` or external `artifact_ref`.
  Replaces the opaque `search_meta` dict.
  For native FTS the artifact is `external`, referencing a content-addressed text record.
- **`SearchRequest`/`SearchResponse` protocol** (foundation): `search()` takes a `SearchRequest` (query, scope, search type, permission-pruned `search_metas`, guarded reader, `SearchLimits`, `find_predicates`) and returns a `SearchResponse`.
  Breaking change to the Phase 1 `SearchProvider`; the single in-tree `DefaultSearchProvider` migrates in this change.
- **Guarded `ContentReader`** (foundation): the VFS-owned content reader is an object, not a bare callable.
  It reads by **`content_hash`** (immutable — no race with concurrent writes, matches the artifact) and enforces `max_content_reads` as a hard ceiling and authorization.
  Used only for the bounded straggler-verification path.
- **`NativeTextSearch` capability** (protocol _and_ SQLite/Postgres implementations, the latter added onto the `phase2-storage` store classes): optional metadata-store capability that indexes content text and answers regex/fulltext by **verifying against the stored text — no blob reads for fresh artifacts** — then expands content matches to visible occurrences.
  Content matched, identity from the VFS-enumerated visible version.
- **Document/occurrence search model**: searchable text persisted in a content-addressed
  `search_text_artifacts` table keyed by `(provider_key, params_hash, content_hash)`; a match on
  content expands through visible versions referencing that content; results report
  path/version of the occurrence.
- **Search-side storage machinery** (on the store classes, owned lifecycle): the `search_text_artifacts` table + migration, in-transaction `index_text`, and text-artifact GC folded into the blob-orphan sweep plus a retired-`params_hash` sweep.
  The stored text is content at blob-level confidentiality (see `design.md`).
- **`FindSearch` predicate expansion**: `find_predicates` on `SearchRequest` supporting name
  pattern, size range, mtime, and live/tombstone type, combined conjunctively.
- **Cold-index failure semantics**: a fresh index returns complete results with no blob reads;
  a bounded number of stragglers (≤ `max_content_reads`) verify via the guarded reader; a cold
  or unavailable index **fails loud** with an actionable reindex/unavailable error — never a
  silent partial result or an unbounded blob-read storm.
- **`SearchMetaReindex` lifecycle**: batch reindex; VFS-owned opportunistic lazy backfill (MAY)
  bounded by `max_content_reads`; `search_meta` copy on rollback (free, because the text
  artifact is content-addressed).

### Out of Scope

- Bloom filter provider (not pursued — see Intent).
- **MongoDB regex/fulltext** — Mongo does not implement `NativeTextSearch` and brute-force whole-scope fallback is deferred, so accelerated regex/fulltext is unavailable on Mongo this phase (glob/find still work).
  Deferred.
- **Whole-scope brute-force search and scope limiting** (CWD-expanding narrowing,
  `search_brute_force_limit`, partial-results metadata) — deferred; cold indexes fail loud
  instead.
- Semantic / embedding search and chunked indexing (future change).
- The metadata/blob adapters themselves, SQLAlchemy Core schema, and tier retention (`phase2-storage`).
  This change extends the SQL store classes with FTS; it does not build them.
- Execution providers (Phase 3).

## Approach

`SearchProvider` is a port; `NativeTextSearch` is an optional metadata-store capability.
The VFS dispatches: if the store offers `NativeTextSearch` for the requested type, use it; else serve glob/find via `DefaultSearchProvider`.
Build foundation first, then wire the capability:

1. Define the `SearchArtifact` envelope and lifecycle states.
2. Migrate the protocol to `SearchRequest`/`SearchResponse`; build the guarded `ContentReader`
   (content-hash-based, budget/auth enforced); migrate `DefaultSearchProvider`.
3. Define the `NativeTextSearch` capability protocol and the content-addressed text model.
4. Add the `search_text_artifacts` table + migration to the `phase2-storage` Core schema, and
   implement `NativeTextSearch` on the SQLite (FTS5) and Postgres (`tsvector` + `pg_trgm`) store
   classes; fold text-artifact GC into the blob-orphan sweep.
5. Wire VFS dispatch to the capability (or glob/find fallback).
6. Expand `FindSearch` predicates.
7. Implement cold-index fail-loud + bounded straggler verification.
8. Add batch reindex, bounded lazy backfill, and rollback `search_meta` copy.

## Open Questions

None.
Index residence (metadata-store capability), identity (content-addressed, document vs occurrence), and cold-index semantics (fail loud) are resolved in `design.md`.
