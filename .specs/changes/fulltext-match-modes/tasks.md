# Tasks: fulltext-match-modes

> Build-dependency order: word representation + Postgres `simple` + migration (the
> substrate) → match-mode enum/field → VFS/session threading → ANY construction on the word
> representation (both backends) → boundary term cap → dispatch/validation → cross-backend
> contract tests → notebook demo.
>
> Every SHALL is paired with at least one evidence-producing test. Foreseeable write-sites
> for the match-mode dispatch: the SQLite ANY path, the Postgres ANY path, the ALL default
> path (both backends), and the non-FULLTEXT ignore path. The in-process straggler FULLTEXT
> path is **removed** (see §12): stale fulltext fails loud, so `match_mode` does not thread
> through any straggler path.
>
> **Implementation status:** sections 3–5 and 7–10 were implemented against the prior
> trigram-fulltext substrate during initial work. The substrate change in sections 1–2
> supersedes them: the SQLite fulltext path must be re-pointed from the trigram table to the
> new word table, the Postgres path from `'english'` to `'simple'`, and the affected tests
> re-grounded on real terms (e.g. `s3`). Treat previously-implemented items as drafts to
> rebase onto the word representation, not as done.

## 1. Word representation — substrate (`FulltextWordRepresentation`)

- [ ] SQLite: add a second FTS5 virtual table (e.g. `search_fts_words`) with
  `tokenize='unicode61'` in `_setup_fts5`, keyed by `(provider_key, params_hash, content_hash)`
  like `search_fts`; have `index_text` write the decoded text into it alongside the trigram
  table, and `delete_text_artifacts` prune it
- [ ] Do NOT bump `params_hash` on either backend — it keys the tokenizer-independent `raw_text` and is shared with REGEX (bumping invalidates regex artifacts and exposes the shared `raw_text` to the retired-`params_hash` GC sweep).
  Bump the informational `SearchArtifact.provider_version` (from `"1"`) at all five construction sites — `sqlite_metadata.index_text`, `postgres_metadata.index_text`, and the three `vfs.py` artifact builders — as a forward marker only (it is not used in `is_usable` and does not retroactively re-tag existing records)
- [ ] Postgres: switch fulltext from `'english'` to `'simple'` at ALL FOUR sites — the `to_tsvector(...)` in both the `@@` predicate and the `ts_rank` score in `_fulltext_search`, AND the two `plainto_tsquery('english', …)` calls in `_build_fulltext_tsquery` (ALL and ANY paths).
  No `params_hash` change and no migration (tsvector is inline); leave the `pg_trgm` regex path unchanged
- [ ] Test (`ShortTermFulltextIsRepresentable`): a FULLTEXT search for `s3` over
  "deploy to s3" / "deploy to archive" returns only the `s3` document on each backend
- [ ] Test (`FulltextMatchesWholeWordsNotSubstrings`): FULLTEXT `cat` does NOT match a
  document containing only "category" (word-token semantics)
- [ ] Test (`RegexStillMatchesSubstrings`): REGEX `cat` DOES match "category" (trigram
  representation unaffected by the fulltext change)
- [ ] Test (provider_version evidence): a newly written `SearchArtifact` carries the bumped
  `provider_version` at all five construction sites (assert the value at
  `sqlite_metadata.index_text`, `postgres_metadata.index_text`, and the three `vfs.py`
  builders), and `is_usable` is unaffected by the change (a record with the old
  `provider_version` but matching `params_hash` is still usable)

## 2. Migration — backfill word index from `raw_text` at init (`DerivedIndexRebuild`)

- [ ] On SQLite store init, backfill `search_fts_words` from existing `search_text_artifacts.raw_text` with an **anti-join** insert keyed on the full `(provider_key, params_hash, content_hash)` identity scoped to the active provider profile (NOT `content_hash` alone — an old/partial row under a different key could otherwise mask a missing current row); insert only rows absent from the word table, under the store lock in a transaction — no blob reads, no content re-decode.
  This is idempotent AND crash-resumable.
  Do NOT use a "count check / run-once-if-empty" guard: it leaves a partial backfill permanently partial, and FTS5 has no unique constraint on `(provider_key, params_hash, content_hash)`, so a naive re-run would double-insert → duplicate `SearchResult`s
- [ ] Document/encode the freshness blind spot: `has_text_artifacts` queries
  `search_text_artifacts` only and cannot see a missing word-table row, so fresh-FULLTEXT
  correctness depends on the backfill completing before serving; an interrupted backfill is
  completed by the resumable anti-join on the next init (the straggler path does NOT cover it)
- [ ] Confirm content whose `raw_text` row is ABSENT falls to the existing straggler/
  `reindex` path (which may blob-read) — unchanged from today; do NOT special-case it
- [ ] Confirm no retired-`params_hash` GC sweep is triggered (params_hash unchanged) and the
  trigram regex artifacts + `raw_text` rows are untouched
- [ ] Test (`WordIndexBackfilledFromRawTextWithoutBlobReads`): given `raw_text` rows from
  before the word index existed, after SQLite init a FULLTEXT search over that content
  returns correct results with zero blob reads (guarded-reader read count == 0)
- [ ] Test (`DerivedIndexRebuildIsIdempotentAndResumable`): a second init does not duplicate
  word-table rows (no duplicate `SearchResult`s), and an init after a simulated partial
  backfill fills only the missing rows

## 3. Model — `FullTextMatchMode` enum (`FulltextMatchMode`) — _implemented; verify_

- [ ] `FullTextMatchMode(Enum)` with `ALL = "all"`, `ANY = "any"` in `src/vfs/models.py`,
  beside `SearchType`
- [ ] Test: members importable from `vfs.models`, equal to themselves; round-trip through
  `SearchRequest.match_mode` confirms the type is the enum, not a string

## 4. Protocol — `match_mode` field on `SearchRequest` — _implemented; verify_

- [ ] `match_mode: FullTextMatchMode = FullTextMatchMode.ALL` on `SearchRequest`; documented
  as FULLTEXT-only, ignored for other types
- [ ] Test (`FulltextMatchModeDefaultIsAll` — default path): no `match_mode` →
  `request.match_mode == ALL`; explicit `ANY` → `ANY`

## 5. VFS & Session threading — _implemented; verify_

- [ ] `match_mode` keyword-only param (default `ALL`) on `vfs.VFS.search` and
  `session.Session.search`, forwarded into `SearchRequest` construction — including the
  `fresh_request` reconstruction in `vfs._native_search`
- [ ] Test: `vfs.search(..., FULLTEXT, match_mode=ANY)` reaches `search_text` with
  `match_mode=ANY` (captured via stub capability, exercising the `fresh_request` path)
- [ ] Test: `vfs.search` without `match_mode` → `ALL`; `session.search(..., match_mode=ANY)`
  delegates with `ANY` intact

## 6. Boundary term-count cap

- [ ] In `vfs.VFS.search`, reject a FULLTEXT query whose whitespace-split term count exceeds
  the maximum (128) with a clear error, before backend query construction (covers SQLite and
  Postgres; 128 is well below the PostgreSQL ~32767 bind-parameter ceiling that ANY approaches)
- [ ] Test (`FulltextRejectsTooManyTerms`): a FULLTEXT query above 128 terms raises the
  boundary error; a query at/below 128 succeeds

## 7. SQLite ANY construction — on the word table (_rebase from trigram draft_)

- [ ] `SQLiteNativeTextSearch._fulltext_search` queries the **word** table, accepts
  `mode: FullTextMatchMode`, and OR-joins double-quoted word tokens (`"tok1" OR "tok2"`) for
  ANY vs AND-join for ALL; double-quote escaping (`"`→`""`) unchanged; empty-query guard
  retained
- [ ] In `vfs._native_search`, **remove** the inline straggler FULLTEXT predicate (see §12):
  a stale/missing FULLTEXT artifact fails loud (`reindex`-required) rather than being
  approximated, so `match_mode` does NOT thread through any straggler path
- [ ] Test (`FulltextMatchAnyRanksUnion` — SQLite): mode=ANY returns the union incl. a
  one-term doc excluded under ALL; both-terms doc ranks first
- [ ] Test (`FulltextMatchAllRequiresEveryTerm` — SQLite): mode=ALL returns only docs with
  every term
- [ ] Test (`RankedFulltextAnyMode` — SQLite): both-terms doc outranks one-term doc (BM25)
- [ ] Test: a query token containing `"` is escaped (`""`) in ANY mode and exercises the
  escaping (only the quoted term can match the asserted doc)
- [ ] Test (straggler fulltext fails loud): a FULLTEXT search whose scope contains any
  stale/missing artifact fails loud with `reindex`-required — no inline approximation for
  either `ALL` or `ANY` (replaces the former inline-straggler-ANY behavior; see §12)

## 8. Postgres ANY construction — on `'simple'` (rebase from the `'english'` draft)

> Integration tests in this group require the Docker-compose Postgres stack.

- [ ] `PostgresNativeTextSearch._fulltext_search` accepts `mode`; for ANY splits on
  whitespace and OR-combines per-term `plainto_tsquery('simple', :tN)` with `||`, reused in
  both the `@@` predicate and `ts_rank`; single-term reduces to one call; zero-term returns
  empty (parity with SQLite)
- [ ] Static (in-sandbox) test: the constructed tsquery fragment + bound params are
  well-formed for 0 / 1 / N terms and carry terms only as bind parameters
- [ ] Test (`FulltextMatchAnyRanksUnion` — Postgres; Docker): union incl. a one-term doc
  excluded under ALL; both-terms doc ranks first
- [ ] Test (`FulltextMatchAllRequiresEveryTerm` — Postgres; Docker): only docs with every term
- [ ] Test (`RankedFulltextAnyMode` — Postgres; Docker): both-terms doc outranks one-term doc
  (`ts_rank`)
- [ ] Test (Docker): single-term ANY == single-term ALL result set

## 9. Dispatch / validation — _implemented; verify_

- [ ] `search_text` reads `request.match_mode` only inside the FULLTEXT branch; GLOB/FIND/
  REGEX unaffected
- [ ] Test (`MatchModeIgnoredForNonFulltext`): `vfs.search` with GLOB/FIND/REGEX and
  `match_mode=ANY` raises no error and returns the same (non-empty) result set as without it

## 10. Cross-backend contract tests

> Integration tests in this group require the Docker-compose Postgres stack and stand up an
> in-memory SQLite word-index store alongside the Postgres store.

- [ ] Keep the exact-REGEX `ResultSetEquivalentToBruteForce` test independent of the SQLite
  fixture (do NOT couple the Postgres regex regression to SQLite word-index availability —
  put cross-backend FULLTEXT assertions in their own test)
- [ ] Test (`AllModeCoherentAcrossBackends`; Docker): same portable-term FULLTEXT query in
  mode=ALL → same path set across SQLite and Postgres (sanity check over a portable-term
  corpus, NOT a guaranteed-identical contract for arbitrary input)
- [ ] Test (`AnyModeCoherentAcrossBackends`; Docker): same portable-term FULLTEXT query in
  mode=ANY → same path set across backends for the portable corpus; each backend
  independently ranks a more-term-matching doc above a fewer-term-matching one (scores not
  compared across backends)
- [ ] Wrap the cross-backend act phase with `pyleak` `no_task_leaks(action="raise")`

## 11. Notebook demo

- [ ] Update `notebooks/02` to demonstrate ALL vs ANY on the same corpus: same multi-term FULLTEXT query in both modes, result sets shown side-by-side.
  Label the Postgres ranker `ts_rank` (not BM25)

## 12. Search correctness floor — self-healing → fail-loud (folded from #5)

> Serves US-2. Removes the always-on self-healing that ladders to no PoC user story while
> keeping the fail-loud trust floor. Net code reduction, not added scope.

- [ ] Drop **query-time lazy backfill** in `vfs._native_search` (the straggler-path `index_text`
  - `update_search_artifact` block); `vfs.reindex()` remains the explicit remedy.
    The write-path in-transaction `index_text` is unchanged (fresh ⇔ current invariant holds).
- [ ] Drop the **`has_text_artifacts` existence re-check** in `vfs._native_search`; the in-DB
  index makes out-of-band record loss a non-state (`object-store-text-index` parked).
- [ ] **FULLTEXT straggler → fail loud:** classification keeps REGEX straggler verification
  (honest line-level `re`); a stale/missing FULLTEXT artifact raises `reindex`-required rather
  than running the inline token predicate (deleted with §7).
- [ ] Remove the now-dead `is_usable` external params (`external_readable` /
  `external_identity_match`) in `models.py`, unreferenced once the existence re-check is gone.
- [ ] Spec deltas (`specs/search/spec.md`): narrow `ColdIndexFailsLoud` / `NativeTextSearchCapability` so straggler verification covers REGEX only and FULLTEXT staleness fails loud; soften `SearchMetaReindex` (versioning) to drop the lazy-backfill `MAY`.
  Each delta requirement carries `Serves: US-2`.
- [ ] Test: fresh FULLTEXT (both modes) authoritative with zero blob reads; any FULLTEXT
  straggler → `reindex`-required (no approximation); REGEX straggler verify + budget fail-loud
  unchanged; no lazy backfill occurs (a 2nd search after a straggler still requires explicit
  `reindex`).

## User-owned

- [ ] Update `CHANGELOG.md` under Unreleased:
  - FULLTEXT now uses a word-tokenized representation (SQLite `unicode61`, Postgres `'simple'`) distinct from the trigram representation used for REGEX; non-stemming / language-neutral.
    **Behavior change:** fulltext matches whole words, not substrings, and no longer stems on Postgres (`databases` ≠ `database`); short terms like `s3` now match correctly.
    Includes a one-time word-index rebuild from stored text (no blob reads) on upgrade.
  - New `FullTextMatchMode` enum (`ALL`/`ANY`); `match_mode` field on `SearchRequest`;
    `vfs.search` / `session.search` gain a `match_mode` kwarg; default `ALL`
    (backward-compatible); `ANY` returns the ranked-OR union.
  - SemVer: MINOR.
