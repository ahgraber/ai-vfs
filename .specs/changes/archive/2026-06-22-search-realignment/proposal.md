# Search Realignment: Word Representation, Match Modes & Self-Healing Cull

**Change name:** `search-realignment` **Date:** 2026-06-14 (scope expanded 2026-06-15; self-healing cull folded in 2026-06-20) **Author:** ahgraber + Claude

## Intent

Three defects motivate this change: two coupled in the FULLTEXT representation, and one in the native-search self-healing machinery.

1. **FULLTEXT runs on the wrong representation.**
   It rides on the _trigram_ index that exists for REGEX acceleration.
   On SQLite a single `search_fts` (`tokenize=trigram`) table backs both regex candidate-pruning and fulltext BM25 ranking; on Postgres fulltext uses an English-stemmed `to_tsvector('english', …)`.
   Trigram matching is substring-based with a three-character floor: a query term shorter than a trigram (e.g. `s3`) produces no tokens and degenerates to an _empty constraint_, so strict-AND silently returns documents that lack the term.
   The two backends also analyze text differently (trigram substrings vs. English lexemes), so no honest cross-backend guarantee is possible.

2. **There is no ranked-OR.**
   FULLTEXT is strict-AND only.
   A user searching `"hello s3"` gets zero results for a document containing only `"hello"`, even though it is clearly relevant.

3. **The native-search self-healing is overbuilt.**
   `vfs._native_search` verifies stragglers via blob reads, lazily backfills, re-checks external-record existence, and approximates FULLTEXT inline — machinery built as if a not-fresh index were a steady-state hazard.
   It is not: indexing is atomic with the version write, so a fresh index is the steady state and stragglers are a migration/index-build transient that `reindex` already owns.
   The machinery pays always-on cost (and returns dishonest FULLTEXT results), and an adversarial review found it was masking a real defect — `copy`/`move` commit a current version without propagating `search_meta`, manufacturing steady-state stragglers.

This change introduces a **representation-per-modality** model, layers a **match-mode** parameter on top of it, and reduces the native path to its correctness floor — a fresh index is authoritative; any straggler fails loud and points at a (path-scoped) `reindex`:

| Modality | Representation             | SQLite                            | Postgres                   |
| -------- | -------------------------- | --------------------------------- | -------------------------- |
| REGEX    | **trigram** (unchanged)    | `search_fts` (`tokenize=trigram`) | `pg_trgm` GIN              |
| FULLTEXT | **word tokens** (new)      | FTS5 `unicode61` table            | `to_tsvector('simple', …)` |
| SEMANTIC | **vector** (named, future) | —                                 | —                          |

The word representation gives true per-term presence semantics (no trigram floor — `s3` is a first-class token) and is **aligned across backends** using non-stemming tokenizers, giving a coherent cross-backend model (same concepts; results may differ in tokenizer detail, not guaranteed identical).
On that representation, callers choose **`ALL`** (strict-AND, backward-compatible default) or **`ANY`** (ranked-OR union).

## User Stories

- **US-1 (effective search) — _Serves north-star P4 (filesystem + code-mode interaction)._**
  As an agent searching my workspace, I want correct whole-word / short-term fulltext and a
  ranked-OR (`ANY`) mode, so relevant documents surface even when not every query term matches.
- **US-2 (trustworthy search) — _Serves north-star P2 (trust scaffolding)._**
  As an agent relying on search, I need search to be authoritative when the index is fresh
  and to **fail loud (reindex)** when it is stale — never an approximation that differs from
  the index — so I can trust what search tells me.
- **US-3 (correct search after file ops) — _Serves north-star P4 (filesystem + code-mode interaction)._**
  As an agent that copies, moves, and rolls back files as part of normal work, I want search
  over the result to be immediately correct and cheap — not silently degraded, not a forced
  reindex — so routine file operations don't quietly break search.

> Delta-spec requirements carry `Serves: US-1` / `US-2` / `US-3` backlinks. This change pilots
> the value-chain directive (the first to ladder to `NORTH-STAR.md`).

## Scope

> Build-dependency order: representation layer (SQLite word index + Postgres `simple`) and
> its migration land first; the match-mode enum/threading and ANY construction sit on top;
> cross-backend contract test and notebook demo last. `design.md` and `tasks.md` follow
> this order.

### In Scope

- **Representation decoupling — FULLTEXT no longer shares the trigram index.**
  - **SQLite:** a new word-tokenized FTS5 table (`tokenize='unicode61'`) for fulltext, populated from the already-stored `raw_text`.
    The trigram `search_fts` table remains, used only for regex.
  - **Postgres:** fulltext switches from `to_tsvector('english', …)` / `plainto_tsquery('english', …)` to the non-stemming `'simple'` config in both the `@@` predicate and the `ts_rank` score.
    The `pg_trgm` regex path is unchanged.
- **Cross-backend tokenizer alignment (non-stemming).**
  `unicode61` and `'simple'` both case-fold and split on word/punctuation boundaries with no stemming and no stop-word removal, so the two backends agree on whole-word terms.
  Residual tokenizer edge cases (diacritic folding, URL/email handling) are _documented_, not promised away.
- **`FullTextMatchMode` enum** (`ALL`, `ANY`) defined in `src/vfs/models.py`, and a `match_mode` field on `SearchRequest` threaded through `vfs.VFS.search` and `session.Session.search`.
  Default `ALL`.
  Applies only to FULLTEXT; ignored (no error) for GLOB/FIND/REGEX.
- **ANY construction on the word representation.**
  SQLite OR-joins double-quoted word tokens (`"tok1" OR "tok2"`); Postgres OR-combines per-term `plainto_tsquery('simple', :tN)` with the `||` operator.
  Ranking via FTS5 BM25 / Postgres `ts_rank`.
- **Coherent cross-backend model** (not a result-equivalence guarantee): both backends expose the same FULLTEXT model — non-stemming word matching, `ALL`/`ANY`, valid sub-trigram terms — so queries behave consistently.
  Results are NOT guaranteed byte-identical; for portable terms the backends SHOULD agree (asserted as a sanity check), and monotonic relevance ordering is asserted per backend (scores not compared across backends).
- **Boundary validation:** a maximum query-term count enforced in `vfs.VFS.search`, bounding
  the dynamic SQL / bind-parameter growth of ANY on both backends.
- **Migration plan:** `params_hash` is NOT bumped (it keys the tokenizer-independent `raw_text` and is shared with regex).
  The SQLite word index is backfilled from the stored `raw_text` at init via a resumable anti-join insert (no blob reads, no content re-decode); content whose `raw_text` is absent falls to the existing straggler/`reindex` path, unchanged.
  Postgres needs no migration (its tsvector is computed inline).
  `SearchArtifact.provider_version` is bumped on new writes as a forward marker satisfying the version-bump policy — but the backfill, not `provider_version`, is what makes the migration correct (existing records are not rewritten).
  The regex/trigram index and its artifacts are untouched.
- **Spec scenarios** `FulltextMatchAnyRanksUnion` and `FulltextMatchAllRequiresEveryTerm`
  using the real short term `s3`, now representable on both backends.
- **Notebook demo:** `notebooks/02` contrasts ALL vs ANY on the same corpus.
- **Additive `NativeTextSearch` protocol note:** the protocol gains `match_mode` via `SearchRequest`; any future implementation honors it when it lands.
  No build-order dependency.
- **Native-search self-healing cull — collapse `_native_search` to classify + fail-loud + fresh-serve.**
  _Serves US-2._
  Classify each in-scope version as _decided_ (identity-current artifact: `ready` answers, `unsupported`/`failed` is a confirmed non-match) or _straggler_ (absent or identity-drifted); **any straggler in scope fails loud** (`ReindexRequiredError`, path-scoped).
  Serve decided/`ready` versions from the index with zero blob reads.
  This **culls** the guarded-reader straggler verification (REGEX _and_ FULLTEXT), the query-time lazy backfill, the `has_text_artifacts` existence re-check, and the inline FULLTEXT token approximation — so `match_mode` no longer threads through any straggler path.
- **`copy` / `move` propagate `search_meta`** from the source version (mirroring `rollback`), so derived versions are fresh with zero reads — the prerequisite that makes "stragglers are migration-only" true (an adversarial review found copy/move otherwise manufacture steady-state stragglers).
  _Serves US-3._
- **`failed` folded into the decided non-match class** (with `unsupported`): an identity-current un-indexable artifact is excluded, not a straggler — so an oversized file does not brick its scope under fail-loud.
- **Path-scoped `reindex` remediation** — fail-loud points at the stale subtree, not the whole namespace.
- **`_blob_gc` reference-check→delete made atomic** + the live-reference invariant asserted (a `content_hash` with a live version reference is never swept) — the GC race the existence re-check incidentally guarded.
- **Dead-code removal:** `has_text_artifacts` (both stores), the `is_usable` external params (`external_readable` / `external_identity_match`), and the straggler-only regex helper if unreferenced.

### Out of Scope

- **SEMANTIC / vector representation** — named as the third modality slot; not built here.
- **Stemming / stop-word fulltext** — deliberately rejected for cross-backend alignment and multilingual neutrality (see `design.md`).
  A future opt-in could add a stemmed profile under a distinct `params_hash` without disturbing this representation.
- **PHRASE / PROXIMITY** match modes — deferred; the enum is the extension point.
- Changing the default from `ALL` to `ANY` — deliberately rejected.
- **REGEX / GLOB / FIND** behavior — unchanged; `match_mode` is a no-op for those types.
- **MongoDB** fulltext — remains unsupported.
- **Brute-force fallback search** (no `NativeTextSearch` capability) — unchanged; it retains the guarded reader and `max_content_reads` budget.
- Any change to the `SearchArtifact` envelope shape, the content→occurrence identity
  contract, or the `object-store-text-index` change's files.

## Approach

Add a dedicated word-tokenized fulltext representation on each backend and align the two on non-stemming tokenizers, leaving trigram in place for regex.
Define `FullTextMatchMode` and thread it from `session.search` → `vfs.search` → `SearchRequest` → each backend's `_fulltext_search`.
ANY OR-joins per-term word matches; ALL keeps strict-AND.
Rank by the backend's native relevance score.
Do not bump `params_hash` (it keys the tokenizer-independent `raw_text` shared with regex); instead build the SQLite word index from `raw_text` by running an idempotent, crash-resumable anti-join on each init (zero blob reads) and signal the behavior change via the informational `SearchArtifact.provider_version`.
Validate a maximum term count at the public boundary.

## Resolved Decisions

- **Stemming:** non-stemming (`unicode61` + `'simple'`), for a coherent cross-backend model
  and multilingual neutrality; stemming is recorded as a rejected alternative and a possible
  future profile (see `design.md`).
- **Tokenizer edge-case parity** (`unicode61` vs `'simple'`): accepted as a documented
  residual; the cross-backend goal is a coherent model, not identical results — backends may
  differ on edge cases (diacritics, URL/email segmentation), and that is expected.
