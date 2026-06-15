# Tasks: fulltext-match-modes

> Build-dependency order: model/enum → SearchRequest field → VFS/session threading →
> SQLite ANY → Postgres ANY → dispatch/validation → cross-backend contract test extension
> → notebook demo.
> Every SHALL is paired with at least one evidence-producing test; foreseeable write-sites
> for the match-mode dispatch are the SQLite ANY path, the Postgres ANY path, the ALL
> default path (both backends), and the non-FULLTEXT ignore path.

## 1. Model — `FullTextMatchMode` enum (`FulltextMatchMode`)

- [ ] Add `FullTextMatchMode(Enum)` with members `ALL = "all"` and `ANY = "any"` to
  `src/vfs/models.py`, alongside `SearchType`
- [ ] Test: `FullTextMatchMode.ALL` and `FullTextMatchMode.ANY` are importable from
  `vfs.models` and compare equal to themselves (`FulltextMatchMode` — enum definition)
- [ ] Test: a `FullTextMatchMode` round-trips through `SearchRequest.match_mode` (import
  and field access confirm the type is the enum, not a string)

## 2. Protocol — `match_mode` field on `SearchRequest`

- [ ] Add `match_mode: FullTextMatchMode = FullTextMatchMode.ALL` to `SearchRequest` in
  `src/vfs/protocols/search.py`; document that it applies only to FULLTEXT searches and
  is ignored for other types
- [ ] Test: constructing `SearchRequest` without a `match_mode` argument yields
  `request.match_mode == FullTextMatchMode.ALL` (`FulltextMatchModeDefaultIsAll` —
  default-value path)
- [ ] Test: constructing `SearchRequest` with `match_mode=FullTextMatchMode.ANY` yields
  `request.match_mode == FullTextMatchMode.ANY`

## 3. VFS & Session threading

- [ ] Add `match_mode: FullTextMatchMode = FullTextMatchMode.ALL` kwarg to
  `vfs.VFS.search`; forward it into `SearchRequest` construction
- [ ] Add `match_mode: FullTextMatchMode = FullTextMatchMode.ALL` kwarg to
  `session.Session.search`; forward it into the `vfs.search` call unchanged
- [ ] Test: calling `vfs.search(..., search_type=FULLTEXT, match_mode=ANY)` produces a
  `SearchRequest` with `match_mode=ANY` that reaches `NativeTextSearch.search_text`
  (verified via a minimal stub/mock of the NativeTextSearch capability that captures the
  received request)
- [ ] Test: calling `vfs.search` without `match_mode` produces a request with
  `match_mode=ALL` (`FulltextMatchModeDefaultIsAll` — threading path, canonical call site)
- [ ] Test (write-site: session threading): calling `session.search(..., match_mode=ANY)` delegates to `vfs.search` with `match_mode=ANY` intact

## 4. SQLite ANY construction

- [ ] In `SQLiteNativeTextSearch._fulltext_search`, accept `mode: FullTextMatchMode` (read
  from `request.match_mode`) and, when `mode=ANY`, OR-join the double-quoted token phrases
  (`"tok1" OR "tok2"`) instead of AND-joining them; the double-quoting rule (internal `"`
  → `""`) is unchanged
- [ ] Test (write-site: SQLite ANY path): a FULLTEXT search with mode=ANY on an SQLite
  store returns every document matching at least one query term, including a document that
  would be excluded in ALL mode (`FulltextMatchAnyRanksUnion` — SQLite)
- [ ] Test (write-site: SQLite ALL path): a FULLTEXT search with mode=ALL on an SQLite
  store returns only documents containing every query term (`FulltextMatchAllRequiresEveryTerm`
  — SQLite)
- [ ] Test: SQLite ANY mode returns results ordered by descending BM25 relevance; a
  document matching both query terms ranks above a document matching only one
  (`RankedFulltextAnyMode` — SQLite)
- [ ] Test: a query token containing a double-quote character is correctly escaped (`""`)
  in SQLite ANY mode — the injection-safe quoting applies in OR mode as in AND mode

## 5. Postgres ANY construction

> Integration tests in this group require the Docker-compose Postgres stack
> (`pytest -m integration_lifecycle` or the `docker` marker per repo convention).

- [ ] In `PostgresNativeTextSearch._fulltext_search`, accept `mode: FullTextMatchMode`
  and, when `mode=ANY`, split the query on whitespace, call
  `plainto_tsquery('english', :tN)` for each term, and combine them with `||`; the `@@`
  match operator and `ts_rank` scoring are unchanged; for a single-term query the
  construction reduces to one `plainto_tsquery` call
- [ ] Test (write-site: Postgres ANY path, integration): a FULLTEXT search with mode=ANY
  on a Postgres store returns every document matching at least one query term, including a
  document that would be excluded in ALL mode (`FulltextMatchAnyRanksUnion` — Postgres;
  requires Docker)
- [ ] Test (write-site: Postgres ALL path, integration): a FULLTEXT search with mode=ALL
  on a Postgres store returns only documents containing every query term
  (`FulltextMatchAllRequiresEveryTerm` — Postgres; requires Docker)
- [ ] Test (integration): Postgres ANY mode returns results ordered by descending ts_rank;
  a document matching both terms ranks above a document matching only one
  (`RankedFulltextAnyMode` — Postgres; requires Docker)
- [ ] Test (integration): a single-term query in ANY mode produces the same result set as
  in ALL mode (single-term degenerate case; requires Docker)

## 6. Dispatch / validation

- [ ] Confirm that `NativeTextSearch.search_text` implementations read `request.match_mode`
  only inside the FULLTEXT branch of their dispatch; GLOB, FIND, and REGEX branches are
  unaffected (read the field but never act on it, or simply do not read it)
- [ ] Test (write-site: non-FULLTEXT ignore path): calling `vfs.search` with
  `search_type=GLOB` (or FIND or REGEX) and `match_mode=ANY` raises no error and returns
  the same results as the same search without a `match_mode` argument
  (`MatchModeIgnoredForNonFulltext`)

## 7. Cross-backend contract test extension

> Integration tests in this group require the Docker-compose Postgres stack.

- [ ] Extend the existing `ResultSetEquivalentToBruteForce` contract test to also run the
  same FULLTEXT query in mode=ALL and assert the same-path-set result across SQLite and
  Postgres (this formalizes the existing scenario; no behavior change, just explicit mode
  coverage)
- [ ] Add a new contract test for mode=ANY: run the same FULLTEXT query with mode=ANY
  against the same corpus on both the SQLite and Postgres backends; assert that the set of
  returned paths is identical across both backends (`AnyModeResultSetEquivalentAcrossBackends`)
- [ ] In the ANY-mode contract test, assert that a document matching both query terms has
  a higher rank position than a document matching only one on each backend independently
  (monotonic ordering check; score values not compared across backends)
- [ ] Wrap the contract test with `pyleak` `no_task_leaks` per project testing policy

## 8. Notebook demo

- [ ] Update `notebooks/02` to demonstrate ALL vs ANY on the same corpus: index a small
  document set, run the same multi-term FULLTEXT query in both modes, and display the
  difference in result sets side-by-side

## User-owned

- Update `CHANGELOG.md` under Unreleased: new `FullTextMatchMode` enum; `match_mode`
  field on `SearchRequest`; `vfs.search` and `session.search` gain `match_mode` kwarg;
  default is `ALL` (backward-compatible); `ANY` returns ranked-OR union.
