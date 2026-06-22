# Tasks: search-realignment

> Build-dependency order: word representation + Postgres `simple` + migration (the
> substrate) → match-mode enum/field → VFS/session threading → ANY construction on the word
> representation (both backends) → boundary term cap → dispatch/validation → cross-backend
> contract tests → notebook demo → native-search self-healing cull (§12).
>
> Every SHALL is paired with at least one evidence-producing test. Foreseeable write-sites
> for the match-mode dispatch: the SQLite ANY path, the Postgres ANY path, the ALL default
> path (both backends), and the non-FULLTEXT ignore path. The in-process straggler path is
> **removed** by the §12 cull: any straggler fails loud (REGEX and FULLTEXT), so `match_mode`
> does not thread through any straggler path.
>
> **Implementation status (reviewed 2026-06-21; substrate landed 2026-06-21):**
>
> All tasks implemented and tested. §1–§11 plus §12 are complete: the `unicode61` word table,
> the Postgres `'english'`→`'simple'` switch, the init-time anti-join backfill, the term cap,
> the ANY/ALL construction rebased onto the word representation, and the cross-backend coherence
> tests are in place. Unit suite (477) and the Postgres + cross-backend integration legs (20)
> pass against the local compose stack — including `ShortTermFulltextIsRepresentable`,
> `FulltextMatchesWholeWordsNotSubstrings`, and `{All,Any}ModeCoherentAcrossBackends` over the
> `s3` corpus on both backends.
>
> The `provider_version` bump touches **four** construction sites — `sqlite_metadata.index_text`,
> `postgres_metadata.index_text`, and the **two** remaining `vfs.py` `unsupported` builders
> (write-path decode error and reindex-path). The §12 cull removed the former lazy-backfill
> builder, so the earlier "five sites / three `vfs.py` builders" count no longer applies.

## 1. Word representation — substrate (`FulltextWordRepresentation`)

- [x] SQLite: add a second FTS5 virtual table (e.g. `search_fts_words`) with
  `tokenize='unicode61'` in `_setup_fts5`, keyed by `(provider_key, params_hash, content_hash)`
  like `search_fts`; have `index_text` write the decoded text into it alongside the trigram
  table, and `delete_text_artifacts` prune it
- [x] Do NOT bump `params_hash` on either backend — it keys the tokenizer-independent `raw_text` and is shared with REGEX (bumping invalidates regex artifacts and exposes the shared `raw_text` to the retired-`params_hash` GC sweep).
  Bump the informational `SearchArtifact.provider_version` (from `"1"` to `"2"`) at all four construction sites — `sqlite_metadata.index_text`, `postgres_metadata.index_text`, and the two `vfs.py` `unsupported` builders (write-path decode error, reindex-path) — as a forward marker only (it is not used in `is_usable` and does not retroactively re-tag existing records). (The §12 cull removed the former lazy-backfill builder, so the count is four sites, not five.)
- [x] Postgres: switch fulltext from `'english'` to `'simple'` at ALL FOUR sites — the `to_tsvector(...)` in both the `@@` predicate and the `ts_rank` score in `_fulltext_search`, AND the two `plainto_tsquery('english', …)` calls in `_build_fulltext_tsquery` (ALL and ANY paths).
  No `params_hash` change and no migration (tsvector is inline); leave the `pg_trgm` regex path unchanged
- [x] Test (`ShortTermFulltextIsRepresentable`): a FULLTEXT search for `s3` over
  "deploy to s3" / "deploy to archive" returns only the `s3` document on each backend
- [x] Test (`FulltextMatchesWholeWordsNotSubstrings`): FULLTEXT `cat` does NOT match a
  document containing only "category" (word-token semantics)
- [x] Test (`RegexStillMatchesSubstrings`): REGEX `cat` DOES match "category" (trigram
  representation unaffected by the fulltext change)
- [x] Test (provider_version evidence): a newly written `SearchArtifact` carries the bumped
  `provider_version` at the construction sites (asserted at `sqlite_metadata.index_text` and
  the `vfs.py` decode-error builder in unit, and `postgres_metadata.index_text` in
  integration), and `is_usable` is unaffected by the change (a record with the old
  `provider_version` but matching `params_hash` is still usable)

## 2. Migration — backfill word index from `raw_text` at init (`DerivedIndexRebuild`)

- [x] On SQLite store init, backfill `search_fts_words` from existing `search_text_artifacts.raw_text` with an **anti-join** insert keyed on the full `(provider_key, params_hash, content_hash)` identity scoped to the active provider profile (NOT `content_hash` alone — an old/partial row under a different key could otherwise mask a missing current row); insert only rows absent from the word table, under the store lock in a transaction — no blob reads, no content re-decode.
  This is idempotent AND crash-resumable.
  Do NOT use a "count check / run-once-if-empty" guard: it leaves a partial backfill permanently partial, and FTS5 has no unique constraint on `(provider_key, params_hash, content_hash)`, so a naive re-run would double-insert → duplicate `SearchResult`s
- [x] Document/encode the freshness blind spot: the straggler classifier reasons over the per-version artifact manifest (`search_meta`) and the presence of a `raw_text` record, neither of which sees a missing word-table row, so fresh-FULLTEXT correctness depends on the backfill completing before serving; an interrupted backfill is completed by the resumable anti-join on the next init (the straggler path does NOT cover it).
  Documented in `_backfill_word_index` and the versioning `DerivedIndexRebuild` delta.
- [x] Confirm content whose `raw_text` row is ABSENT falls to the existing straggler/
  `reindex` path (which may blob-read) — unchanged from today; do NOT special-case it
- [x] Confirm no retired-`params_hash` GC sweep is triggered (params_hash unchanged) and the
  trigram regex artifacts + `raw_text` rows are untouched
- [x] Test (`WordIndexBackfilledFromRawTextWithoutBlobReads`): given `raw_text` rows from
  before the word index existed, after SQLite init a FULLTEXT search over that content
  returns correct results with zero blob reads (guarded-reader read count == 0)
- [x] Test (`DerivedIndexRebuildIsIdempotentAndResumable`): a second init does not duplicate
  word-table rows (no duplicate `SearchResult`s), and an init after a simulated partial
  backfill fills only the missing rows

## 3. Model — `FullTextMatchMode` enum (`FulltextMatchMode`)

- [x] `FullTextMatchMode(Enum)` with `ALL = "all"`, `ANY = "any"` in `src/vfs/models.py`,
  beside `SearchType`
- [x] Test: members importable from `vfs.models`, equal to themselves; round-trip through
  `SearchRequest.match_mode` confirms the type is the enum, not a string

## 4. Protocol — `match_mode` field on `SearchRequest`

- [x] `match_mode: FullTextMatchMode = FullTextMatchMode.ALL` on `SearchRequest`; documented
  as FULLTEXT-only, ignored for other types
- [x] Test (`FulltextMatchModeDefaultIsAll` — default path): no `match_mode` →
  `request.match_mode == ALL`; explicit `ANY` → `ANY`

## 5. VFS & Session threading

- [x] `match_mode` keyword-only param (default `ALL`) on `vfs.VFS.search` and
  `session.Session.search`, forwarded into `SearchRequest` construction — including the
  `fresh_request` reconstruction in `vfs._native_search`
- [x] Test: `vfs.search(..., FULLTEXT, match_mode=ANY)` reaches `search_text` with
  `match_mode=ANY` (captured via stub capability, exercising the `fresh_request` path)
- [x] Test: `vfs.search` without `match_mode` → `ALL`; `session.search(..., match_mode=ANY)`
  delegates with `ANY` intact

## 6. Boundary term-count cap

- [x] In `vfs.VFS.search`, reject a FULLTEXT query whose whitespace-split term count exceeds
  the maximum (128) with a clear error, before backend query construction (covers SQLite and
  Postgres; 128 is well below the PostgreSQL ~32767 bind-parameter ceiling that ANY approaches)
- [x] Test (`FulltextRejectsTooManyTerms`): a FULLTEXT query above 128 terms raises the
  boundary error; a query at/below 128 succeeds

## 7. SQLite ANY construction — on the word table (_rebase from trigram draft_)

- [x] `SQLiteNativeTextSearch._fulltext_search` queries the **word** table, accepts
  `mode: FullTextMatchMode`, and OR-joins double-quoted word tokens (`"tok1" OR "tok2"`) for
  ANY vs AND-join for ALL; double-quote escaping (`"`→`""`) unchanged; empty-query guard
  retained
- [x] `match_mode` threads only through the **fresh** capability path; the inline straggler
  predicate is removed by §12b, so `match_mode` does NOT thread through any straggler path
- [x] Test (`FulltextMatchAnyRanksUnion` — SQLite): mode=ANY returns the union incl. a
  one-term doc excluded under ALL; both-terms doc ranks first
- [x] Test (`FulltextMatchAllRequiresEveryTerm` — SQLite): mode=ALL returns only docs with
  every term
- [x] Test (`RankedFulltextAnyMode` — SQLite): both-terms doc outranks one-term doc (BM25)
- [x] Test: a query token containing `"` is escaped (`""`) in ANY mode and exercises the
  escaping (only the quoted term can match the asserted doc)
- [x] (Straggler fail-loud tests for FULLTEXT and REGEX — including removal of the inline
  `ALL`/`ANY` approximation — are owned by §12b; ANY construction here applies only on the
  fresh path)

## 8. Postgres ANY construction — on `'simple'` (rebase from the `'english'` draft)

> Integration tests in this group require the Docker-compose Postgres stack.

- [x] `PostgresNativeTextSearch._fulltext_search` accepts `mode`; for ANY splits on
  whitespace and OR-combines per-term `plainto_tsquery('simple', :tN)` with `||`, reused in
  both the `@@` predicate and `ts_rank`; single-term reduces to one call; zero-term returns
  empty (parity with SQLite)
- [x] Static (in-sandbox) test: the constructed tsquery fragment + bound params are
  well-formed for 0 / 1 / N terms and carry terms only as bind parameters
- [x] Test (`FulltextMatchAnyRanksUnion` — Postgres; Docker): union incl. a one-term doc
  excluded under ALL; both-terms doc ranks first
- [x] Test (`FulltextMatchAllRequiresEveryTerm` — Postgres; Docker): only docs with every term
- [x] Test (`RankedFulltextAnyMode` — Postgres; Docker): both-terms doc outranks one-term doc
  (`ts_rank`)
- [x] Test (Docker): single-term ANY == single-term ALL result set

## 9. Dispatch / validation

- [x] `search_text` reads `request.match_mode` only inside the FULLTEXT branch; GLOB/FIND/
  REGEX unaffected
- [x] Test (`MatchModeIgnoredForNonFulltext`): `vfs.search` with GLOB/FIND/REGEX and
  `match_mode=ANY` raises no error and returns the same (non-empty) result set as without it

## 10. Cross-backend contract tests

> Integration tests in this group require the Docker-compose Postgres stack and stand up an
> in-memory SQLite word-index store alongside the Postgres store.

- [x] Keep the exact-REGEX `ResultSetEquivalentToBruteForce` test independent of the SQLite
  fixture (do NOT couple the Postgres regex regression to SQLite word-index availability —
  put cross-backend FULLTEXT assertions in their own test)
- [x] Test (`AllModeCoherentAcrossBackends`; Docker): same portable-term FULLTEXT query in
  mode=ALL → same path set across SQLite and Postgres (sanity check over a portable-term
  corpus, NOT a guaranteed-identical contract for arbitrary input)
- [x] Test (`AnyModeCoherentAcrossBackends`; Docker): same portable-term FULLTEXT query in
  mode=ANY → same path set across backends for the portable corpus; each backend
  independently ranks a more-term-matching doc above a fewer-term-matching one (scores not
  compared across backends)
- [x] Wrap the cross-backend act phase with `pyleak` `no_task_leaks(action="raise")`

## 11. Notebook demo

- [x] Update `notebooks/02` to demonstrate ALL vs ANY on the same corpus: same multi-term FULLTEXT query in both modes, result sets shown side-by-side.
  Label the Postgres ranker `ts_rank` (not BM25)

## 12. Native-search self-healing cull

> Serves US-2 (trustworthy search) and US-3 (correct search after file ops). Collapses
> `vfs._native_search` to classify + fail-loud + fresh-serve; the copy/move fix (12a) is the
> prerequisite that makes stragglers migration-only. Net code reduction. Implement 12a first.

### 12a. Copy/move propagate `search_meta` (prerequisite — `SearchMetaReindex`)

- [x] `vfs.copy`: set `search_meta=src_version.search_meta` on the destination `VersionMeta`
  (mirroring `rollback`).
- [x] `vfs.move`: set `search_meta=src_version.search_meta` on the destination `VersionMeta`
  (the tombstone keeps `search_meta={}`).
- [x] Test (`CopyPropagatesSearchMeta`): copy a fresh-indexed file → a matching search returns
  source and destination with zero blob reads and no `ReindexRequiredError`.
- [x] Test (`MoveDestinationPropagatesSearchMeta`): move a fresh-indexed file → matching search
  returns the destination with zero blob reads and no `ReindexRequiredError`; source tombstone absent.
- [x] Test (`RollbackCopiesSearchMeta` — regression): rollback propagation still holds.

### 12b. Collapse `_native_search` (`ColdIndexFailsLoud`, `SearchArtifactEnvelope`)

- [x] Reduce `_native_search` to: classify decided (identity-current `ready` answers;
  `unsupported`/`failed` confirmed non-match) vs straggler (absent/identity-drifted); any
  straggler → `ReindexRequiredError` (path-scoped); else `search_text` over decided/`ready`
  (preserving `match_mode` on the fresh path); a capability error → `IndexUnavailableError`.
- [x] Remove the straggler verify loop, guarded-reader straggler reads, query-time lazy backfill,
  the `has_text_artifacts` existence re-check, and the inline FULLTEXT token predicate.
- [x] Fold identity-current `failed` into the confirmed-non-match branch (excluded, not straggler).
- [x] Test (`FreshIndexCompleteNoBlobReads`): all-fresh scope → complete, zero blob reads.
- [x] Test (`AnyStragglerFailsLoud` — REGEX **and** FULLTEXT): one straggler in scope →
  `ReindexRequiredError`, zero reads, no partial/approximate results (replaces the former
  inline-straggler behavior and the bounded REGEX verify).
- [x] Test (`DecidedNonMatchExcluded`): identity-current `unsupported` and `failed` excluded, no fail-loud.
- [x] Test (`IndexUnavailableFailsLoud`): capability `search_text` raises → `IndexUnavailableError`.
- [x] Test (`SearchPerformsNoLazyBackfill`): after fail-loud nothing is written; a 2nd search
  before `reindex` still fails loud.

### 12c. Path-scoped reindex remediation

- [x] The `ReindexRequiredError` message names a path-scoped `reindex` (the search scope).
- [x] Test: error guidance references the scoped form; `reindex(ns, scope=...)` over the stale
  subtree clears the fail-loud.

### 12d. `_blob_gc` atomicity (`NativeTextSearchStorage`)

- [x] Make `_blob_gc`'s reference check + `delete_text_artifacts`+blob delete atomic (transaction
  where available; re-check references inside the delete on best-effort stores).
- [x] Test (`LiveReferencedContentNeverSwept` — SQLite): a live-referenced `content_hash` is never
  swept; the transaction/lock prevents a mid-operation check→revive→delete interleave.
- [x] Test (`TextArtifactGcFollowsContentOrphan` — regression): a truly orphaned hash is still swept.
- [x] Test (Postgres, Docker): `LiveReferencedContentNeverSwept` holds on the transactional store (`test_postgres_metadata.py::TestPostgresNativeTextSearch::test_live_referenced_content_never_swept`) — passed against the local compose stack.
  Mongo exposes no `NativeTextSearch`, so the text-artifact invariant is N/A there.

### 12e. Dead-code removal

- [x] Remove `has_text_artifacts` from `sqlite_metadata.py`, `postgres_metadata.py`, and its tests.
- [x] Remove the `is_usable` external params (`external_readable`/`external_identity_match`) in
  `models.py`; update `test_search_envelope.py`.
- [x] Remove the straggler-only regex helper (`_straggler_regex_results`); also dropped the now-unused
  `import re` in `vfs.py`.
- [x] Test (`ReadyArtifactUsable` / `ContentHashMismatchIsStale` / `ParamsHashMismatchIsStale`):
  the trimmed `is_usable` still classifies usable/stale correctly without the external params.

## User-owned

- [x] Update `CHANGELOG.md` under Unreleased:
  - FULLTEXT now uses a word-tokenized representation (SQLite `unicode61`, Postgres `'simple'`) distinct from the trigram representation used for REGEX; non-stemming / language-neutral.
    **Behavior change:** fulltext matches whole words, not substrings, and no longer stems on Postgres (`databases` ≠ `database`); short terms like `s3` now match correctly.
    Includes a one-time word-index rebuild from stored text (no blob reads) on upgrade.
  - New `FullTextMatchMode` enum (`ALL`/`ANY`); `match_mode` field on `SearchRequest`;
    `vfs.search` / `session.search` gain a `match_mode` kwarg; default `ALL`
    (backward-compatible); `ANY` returns the ranked-OR union.
  - Native search no longer self-heals at query time: a fresh index is authoritative and any
    straggler fails loud (`ReindexRequiredError`) pointing at a path-scoped `reindex`; query-time
    straggler verification, lazy backfill, the external-record existence re-check, and the inline
    FULLTEXT approximation are removed.
  - `copy`/`move` now propagate `search_meta`, so derived files are immediately searchable without
    reindex (regression fix; previously they were perpetual stragglers).
  - Blob GC reference-check and deletion are now atomic (a live-referenced `content_hash` is never
    swept).
  - SemVer: MINOR.
