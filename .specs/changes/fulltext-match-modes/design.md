# Design: Fulltext Match Modes

> Change: `fulltext-match-modes`
> Date: 2026-06-14

## Context

Both `NativeTextSearch` backends use strict-AND semantics today:

- **SQLite FTS5** (`sqlite_metadata.py` `_fulltext_search`): tokens are double-quoted into FTS5 phrases and AND-joined (`"tok1" "tok2"`).
  The double-quoting is deliberate: it treats each token as a literal phrase rather than an FTS5 boolean expression, preventing operator injection (e.g. `c++`, `foo OR bar` do not become syntax errors or Boolean OR).
- **Postgres** (`postgres_metadata.py` `_fulltext_search`): uses
  `plainto_tsquery('english', :query)`, which lexes the input as a sequence of words
  combined with AND and never raises on malformed input — the no-raise property the
  implementation relies on for robustness.

Neither backend offers ranked-OR today.
A `search_text` call on `NativeTextSearch` receives a `SearchRequest`; the request already bundles all per-search context, making it the natural home for a match-mode field.

`SearchType` and all other search-domain enums live in `src/vfs/models.py`; the new enum
goes there. `SearchRequest` lives in `src/vfs/protocols/search.py`.

## Decisions

### Decision: Enum over bool

**Chosen:** A two-member `FullTextMatchMode` enum (`ALL`, `ANY`) in `src/vfs/models.py`.

**Rationale:** A bool (`strict: bool = True`) would work for two values but cannot extend to `PHRASE` or `PROXIMITY` without a breaking change.
An enum makes the extension point explicit and keeps call sites readable (`mode=FullTextMatchMode.ANY` vs `strict=False`).
The enum sits beside `SearchType` in `models.py`, consistent with existing search-domain model placement.

**Alternatives considered:**

- `strict: bool` field on `SearchRequest`: expressive for two values but a dead end if
  PHRASE or PROXIMITY modes are added; rejected.

### Decision: Default ALL — backward-compatible, opt-in ANY

**Chosen:** `FullTextMatchMode.ALL` is the default.
Every existing FULLTEXT caller and every existing spec scenario is unaffected.
`ANY` is opt-in.

**Rationale:** Flipping the default to `ANY` would silently change every existing FULLTEXT caller's result set.
Pre-1.0 with existing contract tests that exercise the strict-AND path, that would break tests without corresponding intent.
The user's expectation of ranked-OR is better served by explicit opt-in that can be demonstrated and then, if desired, made the new default in a future MINOR bump with a documented migration note.
The default decision is load-bearing: it is what makes this change backward-compatible.

**Alternatives considered:**

- Default `ANY` ("more intuitive for keyword search"): would silently change the result
  set for every existing FULLTEXT caller; existing contract tests and callers in
  `notebooks/02` would need coordinated updates; rejected pre-1.0 to avoid silent
  behavioral churn.

### Decision: `match_mode` on `SearchRequest`, threaded through `vfs.search` and `session.search`

**Chosen:** Add `match_mode: FullTextMatchMode = FullTextMatchMode.ALL` to `SearchRequest` as an optional field.
Add the same parameter (default `ALL`) to `vfs.VFS.search` and `session.Session.search`, where it is forwarded into `SearchRequest` construction.
The `NativeTextSearch.search_text` implementations read it from `request.match_mode`.

**Rationale:** `SearchRequest` already bundles all per-search context; adding a field there is the minimal consistent extension.
Threading through `vfs.search` and `session.search` keeps the public API consistent: callers do not need to construct a `SearchRequest` directly.

The field applies only to `FULLTEXT`; for GLOB, FIND, and REGEX it is present but ignored.
Documentation at the field and at `vfs.search`/`session.search` states this.
No error is raised for non-FULLTEXT types with a non-default mode — imposing an error would make it harder to build generic wrappers that always pass a mode.

**Alternatives considered:**

- A separate `fulltext_match_mode` kwarg that is only accepted when `search_type=FULLTEXT`:
  avoids the "ignored for other types" documentation burden, but makes the API asymmetric
  and harder to wrap generically; rejected.

### Decision: SQLite ANY construction — OR-joined double-quoted token phrases

**Chosen:** In `_fulltext_search`, when `mode=ANY`, OR-join the same double-quoted token phrases the AND path uses:

```text
"tok1" OR "tok2" OR "tok3"
```

The double-quoting rule is identical to the AND path (internal `"` → `""`), so user input is treated as literal phrases and FTS5 operator injection is prevented.
The `OR` keyword is a bare ASCII string inserted by the implementation, not part of user input.
BM25 ranking (`ORDER BY rank`) already assigns higher scores to documents matching more and rarer terms, so the ranked-OR contract is satisfied without any additional scoring machinery.

**Rationale:** The AND path's injection-safety analysis (from the `_fulltext_search` docstring) holds unchanged for the OR variant because the construction is identical except for the join string.
No new escaping surface is introduced.

**Alternatives considered:**

- Pass the raw query directly (without token splitting and re-quoting): FTS5 would
  interpret `OR`, `AND`, `NOT`, `-`, `*` as operators; user input like `foo OR bar`
  would produce different results depending on its content — the injection the current
  design avoids; rejected.
- Use FTS5 `NEAR` or other advanced syntax for PHRASE mode: deferred (not in this change).

### Decision: Postgres ANY construction — per-term `plainto_tsquery` OR-combined with `||`

**Chosen:** For `mode=ANY`, split the query on whitespace, call
`plainto_tsquery('english', term)` for each term, and combine them with the tsquery
`||` (OR) operator:

```sql
plainto_tsquery('english', :t0) || plainto_tsquery('english', :t1) || …
```

The `@@` match operator and `ts_rank` scoring are unchanged; only the tsquery argument changes.
For a single-term query the construction reduces to a single `plainto_tsquery` call.

**Rationale:** `plainto_tsquery` never raises on malformed input (unlike `to_tsquery`, which raises on punctuation or reserved syntax) — it normalizes input to a safe lexeme sequence.
Combining per-term queries with `||` gives true tsquery OR: a document matches if any term's tsquery matches its `tsvector`.
`ts_rank` over the OR-combined tsquery naturally scores documents matching more and rarer terms higher, satisfying the ranked-OR contract.

**Alternatives considered:**

- `websearch_to_tsquery('english', :query)`: accepts an explicit `or` keyword in user
  input but its default between bare terms is still AND; a user who passes `"hello s3"`
  without `or` still gets AND semantics; does not address the bug without requiring
  callers to reformat their query strings; rejected.
- `to_tsquery('english', :term1 || ' | ' || :term2)`: builds the OR expression as a
  string; `to_tsquery` raises on malformed individual terms (punctuation, reserved words)
  — it does not have the no-raise robustness of `plainto_tsquery`; rejected.
- Single `plainto_tsquery` on the full query with a wrapper `websearch_to_tsquery` that
  uses `or` separator: same problem as the first alternative above; rejected.

### Decision: Result and ranking contract for ANY

**Chosen:** For `mode=ANY`, the set of returned visible documents is the union of per-term matches (every visible document matching at least one query term).
Results are ordered by descending relevance score (BM25 on SQLite, `ts_rank` on Postgres).
Result-identity (content→visible-occurrence expansion) is unchanged.

**Rationale:** The contract is the minimal extension of the existing ranked-fulltext contract (`RankedFulltext`) to cover multi-term OR.
Scores are backend-specific; the cross-backend equivalence test asserts set membership and monotonic ordering, not score parity — mirroring how the existing `ResultSetEquivalentToBruteForce` requirement handles ranking.

### Decision: Cross-backend equivalence scope for ANY

**Chosen:** For an ANY-mode FULLTEXT query the set of matching paths SHALL be identical across SQLite and Postgres.
The test asserts set-equality for path membership and that both backends order a document matching more query terms above one matching fewer; exact scores are not compared.

Brute-force does not participate in the ANY equivalence test: the `DefaultSearchProvider` regex path can approximate OR by running separate regex queries, but there is no single-pass brute-force FULLTEXT equivalent for ranked-OR.
The equivalence test covers the two native backends only.

**Rationale:** The existing `ResultSetEquivalentToBruteForce` requirement covers ALL mode with a brute-force baseline for regex/fulltext.
ANY mode has no brute-force equivalent (no existing ranked-OR implementation to compare against), so the equivalence test is scoped to the two native backends.
This is a narrower but still meaningful cross-implementation contract.

### Decision: `object-store-text-index` cross-change note — additive, no dependency

**Chosen:** The `NativeTextSearch` protocol gains `match_mode` via `SearchRequest`.
Any future implementation (including `ObjectStoreTextIndex` from the in-progress `object-store-text-index` change) must honor `match_mode` when it merges.
For the object-store index, `ANY` mode would OR-join query terms in the BM25 scoring pass over live segments.
No code in the `object-store-text-index` change directory is modified by this change.

**Rationale:** `SearchRequest` is the shared contract surface; both changes consume it.
Because `match_mode` has a default value, the `object-store-text-index` provider already compiles correctly after this change lands — it will receive the field and silently use the `ALL` default until its own implementation explicitly branches on `match_mode`.
The whichever change merges second adds the explicit branch.
No build-order dependency is created.

## Architecture

```text
   session.search(query, scope, FULLTEXT, match_mode=ANY)
         │
         ▼
   vfs.search(namespace_id, query, scope, FULLTEXT,
              principal_id=..., match_mode=ANY)
         │
         │  builds SearchRequest(search_type=FULLTEXT,
         │                       match_mode=FullTextMatchMode.ANY, ...)
         ▼
   NativeTextSearch.search_text(request, visible_version_ids)
         │
         ├─► SQLite: _fulltext_search(query, ch_to_entries, mode=ANY)
         │     OR-join: "tok1" OR "tok2"   (injection-safe double-quoting preserved)
         │     ORDER BY bm25 rank  (higher = more/rarer terms matched)
         │
         └─► Postgres: _fulltext_search(query, ch_to_entries, mode=ANY)
               plainto_tsquery(:t0) || plainto_tsquery(:t1)
               ts_rank scoring  (higher = more/rarer terms matched)

   Match mode applies only to FULLTEXT dispatch; GLOB/FIND/REGEX ignore it.
   Result-identity (content → visible occurrences) unchanged across modes.
```

## Risks

- **Injection via the `OR` join string (SQLite)**: the `OR` keyword is inserted by the implementation as a bare ASCII string between double-quoted token phrases; it is not derived from user input.
  The risk is identical to the existing AND path (the join string was `" "` before), so no new injection surface exists.
- **Single-term query construction (Postgres)**: for a one-token query, the per-term loop produces a single `plainto_tsquery` call — identical to the ALL path.
  No degenerate behavior.
- **Postgres ANY scores differ from SQLite BM25**: `ts_rank` and BM25 are different functions; relative ordering may differ for edge cases.
  The cross-backend equivalence test asserts set membership and monotonic ordering only, not score parity — consistent with how the existing fulltext tests handle this difference.
- **`object-store-text-index` ignores match_mode until it adds the branch**: after this change lands, the object-store provider silently uses ALL semantics for any mode (the default).
  This is acceptable: the object-store provider is not yet implemented, and the `NativeTextSearch` protocol docstring will document the obligation.
