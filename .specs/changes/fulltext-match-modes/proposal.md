# Fulltext Match Modes: ALL (strict-AND) and ANY (ranked-OR)

**Change name:** `fulltext-match-modes` **Date:** 2026-06-14 **Author:** ahgraber + Claude

## Intent

FULLTEXT search today uses strict-AND semantics on both backends: every query term must appear in a document for it to match.
A user who searches "hello s3" gets zero results for a document containing only "hello", even though it is clearly relevant.
A ranked-OR mode — return every document matching _at least one_ query term, ranked so documents matching more and rarer terms appear first — is the expected behavior for a keyword search.

This change adds a **match-mode** parameter to the FULLTEXT path so callers can choose between the current ALL behavior (strict-AND, backward-compatible default) and the new ANY behavior (ranked-OR).
It does not change GLOB, FIND, or REGEX dispatch.

## Scope

> Build-dependency order: model/enum definition → SearchRequest threading → per-backend
> construction → dispatch/validation → cross-backend contract test → notebook demo.
> `design.md` and `tasks.md` follow this order.

### In Scope

- **`FullTextMatchMode` enum** (model, foundation): `ALL` (strict-AND, default) and `ANY`
  (ranked-OR), defined in `src/vfs/models.py` alongside `SearchType`.
- **`match_mode` field on `SearchRequest`** (protocol): optional, typed `FullTextMatchMode`, default `ALL`.
  Applies only to `FULLTEXT` searches; ignored (and documented as ignored) for all other search types.
- **`match_mode` kwarg threading** through `vfs.VFS.search` and `session.Session.search`,
  defaulting to `ALL` at both call sites.
- **SQLite ANY construction**: OR-join the same double-quoted token phrases the AND path
  uses (`"tok1" OR "tok2"`) so user input stays literal and FTS5 operator injection is
  not possible; BM25 ranking orders results naturally by relevance.
- **Postgres ANY construction**: per-term `plainto_tsquery` calls combined with the `||`
  tsquery OR operator, preserving the no-raise/injection-safe property of the existing
  `plainto_tsquery` path.
- **Result/ranking contract for ANY**: return every visible document matching at least one
  query term, ordered by descending relevance score (BM25 / ts_rank).
- **Cross-backend equivalence for ANY**: for an ANY-mode FULLTEXT query the set of matching
  paths SHALL be identical across SQLite and Postgres.
- **Additive contract note for `object-store-text-index`**: the `NativeTextSearch` protocol gains `match_mode` via `SearchRequest`; any future implementation (including the object-store index) must honor it when it lands.
  No build-order dependency; whichever change merges second adapts.
- **Two new delta-spec scenarios**: `FulltextMatchAnyRanksUnion` and
  `FulltextMatchAllRequiresEveryTerm`.
- **Notebook demo**: `notebooks/02` updated to contrast ALL vs ANY on the same corpus.

### Out of Scope

- PHRASE or PROXIMITY match modes — deferred; the enum provides the extension point.
- Changing the default from ALL to ANY — deliberately rejected (see design.md).
- REGEX, GLOB, or FIND behavior — unchanged; `match_mode` is a no-op for those types.
- Any changes to the `object-store-text-index` change's files.
- Any changes to the `SearchArtifact` envelope or GC machinery.
- Semantic search.

## Approach

Define `FullTextMatchMode` in `models.py` as a two-member enum.
Add it as an optional field (default `ALL`) on `SearchRequest`.
Thread it from `session.search` → `vfs.search` → `SearchRequest` construction → the `_fulltext_search` dispatch in each `NativeTextSearch` implementation.

For SQLite, swap the AND-join (`" ".join(...)`) for an OR-join
(`" OR ".join(...)`) when mode is `ANY`; the same double-quoting of tokens applies, so
injection safety is identical to the AND path.

For Postgres, replace `plainto_tsquery('english', :query)` with a per-term construction that OR-combines `plainto_tsquery` results using the `||` tsquery operator: `plainto_tsquery('english', term1) || plainto_tsquery('english', term2) || …`.
Each `plainto_tsquery` is safe against malformed input; the `||` operator is standard tsquery boolean OR.
For a single-term query the construction reduces to a single `plainto_tsquery` call (no change in behavior).

Result-set identity (content→visible-occurrence expansion) is unchanged.
Scores in ANY mode come from the same ranking functions as ALL mode (BM25 / ts_rank), so more-relevant documents rank higher naturally.

## Open Questions

None.
Default, per-backend construction, and equivalence scope are resolved in `design.md`.
