# Design: Search Realignment — Word Representation, Match Modes & Self-Healing Cull

> Change: `search-realignment`
> Date: 2026-06-14 (scope expanded 2026-06-15; self-healing cull folded in 2026-06-20)

## Context

The system has two text-search modalities backed by metadata-resident indexes, plus a third (semantic) on the roadmap.
Each modality wants a _different representation_ of the same stored text:

- **REGEX** wants **trigrams** — substring/literal matching, language-neutral, used to
  prune candidates before in-process `re` verification.
- **FULLTEXT** wants **word tokens** — per-term presence and lexical relevance ranking.
- **SEMANTIC** (future) wants **vectors**.

Today the FULLTEXT modality does not have its own representation; it borrows whatever the backend already has:

- **SQLite** (`sqlite_metadata.py`): one `search_fts` virtual table with `tokenize='trigram'` backs _both_ regex pruning (`search_fts MATCH '"literal"'`) and fulltext BM25 (`ORDER BY rank`).
  Trigram fulltext is substring-based and cannot represent a term shorter than three characters: `"s3"` produces no trigrams, so as a quoted FTS5 phrase it degenerates to an empty constraint.
  Empirically, strict-AND `"hello" "s3"` returns _every_ document containing `hello` — including ones lacking `s3` — directly violating ALL semantics.
- **Postgres** (`postgres_metadata.py`): regex uses a `pg_trgm` GIN index on `raw_text`; fulltext computes `to_tsvector('english', raw_text)` inline (no stored tsvector column) and matches `plainto_tsquery('english', :query)`.
  The English config stems (`databases`→`database`) and removes stop-words (`the`→∅).

Because trigram-substring (SQLite) and English-lexeme (Postgres) analyses differ, any cross-backend "same result set" guarantee for FULLTEXT is false in general (stop-words, stemming, and substrings each diverge).
Neither backend offers ranked-OR.

The decoded text itself is already stored once, content-addressed, in `search_text_artifacts.raw_text`, keyed by `(provider_key, params_hash, content_hash)`.
The trigram FTS5 table and (on Postgres) the inline tsvector are _derived_ from that text.
A new word-tokenized fulltext index is therefore also derivable from `raw_text` **without reading the blob store** — the key fact that makes the migration cheap.

`SearchType` and the search-domain enums live in `src/vfs/models.py`; `SearchRequest` lives
in `src/vfs/protocols/search.py`.

## Decisions

### Decision: One representation per search modality

**Chosen:** Give each modality the representation it needs, and keep them separate: **trigram → REGEX**, **word tokens → FULLTEXT**, **vector → SEMANTIC (future)**.
FULLTEXT stops borrowing the trigram index.

**Rationale:** Conflating fulltext onto the regex trigram index is the single root cause of both the short-term ALL/ANY violation and the cross-backend divergence.
Separating representations fixes both at the source rather than papering over symptoms (e.g. choosing test terms that avoid the trigram floor, or a raw-text substring fallback).
Trigram remains the right tool for regex (literal pruning) and is _more_ language-neutral than any stemmed word index, so it is retained unchanged for that modality.
The vector slot is named now so the model is complete and the third modality has an obvious home later.

**Alternatives considered:**

- _Keep fulltext on trigram and patch the floor_ (raw-text substring fallback for sub-3-char
  terms): fixes only the short-term symptom on SQLite, leaves fulltext substring-based and
  still divergent from Postgres lexemes; rejected as a stopgap that the representation split
  obviates.
- *Unify by switching SQLite fulltext to the trigram-shared table's tokenizer on Postgres
  too*: not possible without breaking regex pruning, which depends on trigram; rejected.

### Decision: SQLite gains a dedicated `unicode61` word FTS5 table for fulltext

**Chosen:** Add a second FTS5 virtual table (e.g. `search_fts_words`) with `tokenize='unicode61'`, populated from `raw_text` alongside the existing trigram table.
Fulltext (`_fulltext_search`) queries the word table; regex continues to use the trigram table.
`index_text` writes the decoded text into both derived indexes.

**Rationale:** `unicode61` (the default FTS5 tokenizer) splits on word/punctuation boundaries, case-folds, and has **no minimum token length** — `s3` is a first-class token — so per-term presence (ALL/ANY) is exact.
`unicode61` predates the trigram tokenizer in SQLite, so availability is strictly better than today's trigram requirement (≥ 3.34).
The cost is a second FTS5 index (storage + write amplification on `index_text`); acceptable for correct fulltext semantics.

**Alternatives considered:**

- FTS5 `porter` tokenizer (unicode61 + Porter stemming): better English recall but
  diverges from Postgres (FTS5 Porter ≠ Snowball English) and is English-centric; rejected
  here, see "non-stemming" below.
- One FTS5 table with both tokenizers: FTS5 binds one tokenizer per table; not possible.

### Decision: Postgres fulltext uses the non-stemming `'simple'` config

**Chosen:** Replace `to_tsvector('english', …)` / `plainto_tsquery('english', …)` with the `'simple'` config in both the `@@` predicate and the `ts_rank` score.
No stored column changes (tsvector is computed inline today and remains so).
The `pg_trgm` regex path is untouched.

**Rationale:** `'simple'` case-folds and tokenizes on word boundaries with no stemming and no stop-word removal — the closest Postgres analog to SQLite `unicode61`.
This removes the two largest cross-backend divergence sources (stemming and stop-words) at the cost of a one-line query change, no migration on the Postgres side (tsvector is inline).

**Alternatives considered:**

- Keep `'english'`: better English relevance (stemming) but keeps Postgres fundamentally
  divergent from SQLite and English-centric; rejected for alignment + multilingual reasons.
- pg_trgm `similarity()` / `word_similarity()` for fulltext: would mirror SQLite trigram
  most closely and reuse the regex GIN index, but trigram similarity is fuzzy string
  overlap, not term-frequency relevance — it breaks the "more/rarer terms rank higher"
  contract and makes ANY-union ranking awkward; rejected. `ts_rank` over `'simple'` keeps
  real lexical relevance.

### Decision: Non-stemming, language-neutral fulltext

**Chosen:** Both backends use **non-stemming** word tokenization (`unicode61`, `'simple'`).
No stemming, no stop-word removal, language-agnostic.

**Rationale:** This is the load-bearing alignment decision.
It (a) maximizes cross-backend agreement — the two tokenizers then differ only in edge cases, not in core lexeme production; and (b) matches the multilingual neutrality that motivated trigram for regex.
Stemming would re-introduce divergence (the two stemmers are not identical) and bias the system toward English, stemming incorrectly and over-dropping for other languages (and word tokenizers cannot segment CJK without ICU).
The trade-off is weaker English recall (`databases` does not match `database`); this is accepted, and a stemmed profile remains an explicit future option under a distinct `params_hash`.

**Alternatives considered:**

- Stemming (`porter` + `english`): better English recall; rejected for divergence +
  English-centrism; revisit as a future opt-in profile, not a default.

### Decision: Enum over bool for match mode

**Chosen:** A two-member `FullTextMatchMode` enum (`ALL`, `ANY`) in `src/vfs/models.py`, beside `SearchType`.

**Rationale:** A bool (`strict: bool`) covers two values but is a dead end for `PHRASE`/ `PROXIMITY`.
The enum makes the extension point explicit and call sites readable (`mode=FullTextMatchMode.ANY`).

### Decision: Default ALL — backward-compatible, opt-in ANY

**Chosen:** `FullTextMatchMode.ALL` is the default; `ANY` is opt-in.

**Rationale:** Flipping the default would silently change every existing FULLTEXT caller's result set.
ALL-by-default keeps the change backward-compatible; ANY can become the default later via a documented MINOR bump if desired. (Note: the representation switch _does_ change ALL-mode results versus today — see Migration — because fulltext now uses word tokens, not trigram substrings; the `match_mode` _default_ is what stays compatible, not the underlying analyzer.)

### Decision: `match_mode` on `SearchRequest`, threaded through `vfs.search` / `session.search`

**Chosen:** Add `match_mode: FullTextMatchMode = FullTextMatchMode.ALL` to `SearchRequest`, and the same keyword-only parameter (default `ALL`) to `vfs.VFS.search` and `session.Session.search`, forwarded into `SearchRequest` construction.
Backends read `request.match_mode`.
The field applies only to FULLTEXT; for GLOB/FIND/REGEX it is present but ignored, and specifying it raises no error.

**Rationale:** `SearchRequest` already bundles per-search context; threading through the public methods keeps callers from constructing requests directly.
No-error-for-other-types keeps generic wrappers simple.

**Write-sites (threading):** the canonical `SearchRequest(...)` in `vfs.search`, the `fresh_request` reconstruction in `vfs._native_search`, and each backend's `_fulltext_search`.
The in-process straggler verification is **removed** (see "Native-search self-healing cull" below), so `match_mode` does not thread through any straggler path.

### Decision: SQLite ANY construction — OR-joined double-quoted word tokens

**Chosen:** In `_fulltext_search` against the word table, when `mode=ANY`, OR-join the same double-quoted token phrases the AND path uses: `"tok1" OR "tok2"`.
Internal `"`→`""` quoting is unchanged; `OR` is a bare ASCII literal inserted by the implementation, not user input, so no FTS5 operator-injection surface is added.
BM25 `ORDER BY rank` orders results by relevance score (on typical corpora, matching more/rarer terms scores higher — not a universal monotonic guarantee).

**Rationale:** The injection-safety analysis of the AND path holds for OR — the construction differs only in the join string.
Tokens are now `unicode61` words rather than trigram phrases, but the quoting/injection reasoning is identical.

### Decision: Postgres ANY construction — per-term `plainto_tsquery('simple', …)` OR-combined with `||`

**Chosen:** For `mode=ANY`, split the query on whitespace, call
`plainto_tsquery('simple', :tN)` per term, and combine with `||`:

```sql
plainto_tsquery('simple', :t0) || plainto_tsquery('simple', :t1) || …
```

The `@@` operator and `ts_rank` scoring are unchanged; a single-term query reduces to one `plainto_tsquery` call.
A zero-term (empty/whitespace) query returns an empty response, matching SQLite's `if not tokens` guard.
User terms are carried only as bound parameters.

**Rationale:** `plainto_tsquery` never raises on malformed input. `||` is true tsquery OR.
`'simple'` (vs `'english'`) is the only change from the prior ANY decision and is what
aligns Postgres lexemes with SQLite `unicode61` words.

**Alternatives considered:** `websearch_to_tsquery` (defaults bare terms to AND),
`to_tsquery` (raises on malformed terms) — both rejected, same reasoning as before.

### Decision: Result and ranking contract

**Chosen:** ALL returns documents containing every query term; ANY returns the union of per-term matches.
Both order by descending relevance (FTS5 BM25 / Postgres `ts_rank`).
Result-identity (content→visible-occurrence expansion) is unchanged.

### Decision: Coherent cross-backend model — not a result-set equivalence guarantee

**Chosen:** The cross-backend goal is a **coherent capability model**, not byte-identical results.
Both backends expose the same model — two representations (trigram → REGEX/substring, word tokens → FULLTEXT), non-stemming word matching, `ALL`/`ANY` modes, and valid sub-trigram terms — so a query behaves the same way _conceptually_ on either backend.
Exact matching-path sets are NOT guaranteed identical: where the tokenizer implementations differ (diacritic folding, URL/email/host segmentation), results may differ.

For **portable terms** (whole words `unicode61` and `'simple'` segment identically) over fresh-indexed content the two backends SHOULD agree, and tests MAY assert agreement over such corpora as a sanity check — but this is a sanity check, not a guaranteed contract for arbitrary input.
Results are ordered by descending backend relevance score; on a controlled corpus a document matching more/rarer terms ranks above one matching fewer, asserted per backend independently as a test on that corpus (not a universal monotonic guarantee).
Exact scores are not compared (BM25 ≠ `ts_rank`).

Brute-force does not participate in the FULLTEXT assertions: there is no single-pass brute-force ranked-fulltext baseline.
The existing exact-REGEX `ResultSetEquivalentToBruteForce` contract is unchanged (regex paths verify with real `re`/raw-text and remain three-way equivalent).

**Rationale:** A strict "identical path set SHALL" across two independent tokenizers is false in general and was a self-imposed promise, not a product requirement — the requirement is a _coherent mental model_ the user can reason about, with implementation free to differ.
Non-stemming word matching is what makes the model coherent (consistent behavior, multilingual-neutral); the residual tokenizer edge cases are documented rather than asserted away.
This keeps the cross-backend goal honest and removes the over-claim that drove unnecessary scope.

### Decision: Migration — build the word index from `raw_text` at init; do NOT bump `params_hash`

**Chosen:** `params_hash` is deliberately **not** bumped on either backend.
`params_hash` keys the stored `search_text_artifacts.raw_text` records, which are tokenizer-independent (decoded text); the trigram and word indexes are both _derived_ from that same text, and `params_hash` is **shared by REGEX and FULLTEXT** (there is one provider-level hash).
Bumping it would wrongly invalidate the regex artifacts and make the retired-`params_hash` GC sweep delete the shared `raw_text` rows that regex still uses and that the fulltext rebuild reads from.
Adding the word representation changes no stored, content-addressed record, so no keying bump is warranted.

- **SQLite:** on store init (`_setup_fts5`), create the word FTS5 table and backfill it from existing `search_text_artifacts.raw_text` with an **anti-join** insert keyed on the full identity `(provider_key, params_hash, content_hash)` scoped to the active provider profile — insert the decoded text for every such row present in `search_text_artifacts` but **absent from the word table** — run under the store lock in a transaction. (Keying on `content_hash` alone is wrong: an old/partial row under a different key could mask a missing current row.) This is both idempotent and crash-resumable: a re-run (after a crash or interrupted init) inserts only what is still missing, never duplicates.
  A plain "backfill once if empty / count check" is NOT acceptable — it would leave a partially-built index permanently partial, and FTS5 has no unique constraint on `(provider_key, params_hash, content_hash)`, so re-inserting would create duplicate rows and hence duplicate `SearchResult`s.
  No blob reads, no content re-decode.
  Because `params_hash` is unchanged, every version's `search_meta` artifact stays fresh, so the VFS classifier routes existing content to `search_text`, which queries the word table.
- **Postgres:** nothing to migrate — the tsvector is computed inline at query time, so
  `'english'`→`'simple'` is purely a query change with no stored state.

The trigram regex index, the `raw_text` records, and per-version `search_meta` are all untouched; no retired-`params_hash` GC sweep is triggered.

**Freshness blind spot (load-bearing).**
The straggler classifier proves index presence via the per-version artifact manifest (`search_meta`), which reflects the `search_text_artifacts` record only and has **no visibility into the word table**.
A `content_hash` with a `raw_text` row therefore classifies as _fresh_ regardless of whether its word-table row exists — so if the backfill has not completed, `_fulltext_search` queries an incomplete word table and silently returns empty/wrong fulltext with no straggler fallback.
**Fresh-fulltext correctness depends on the init backfill having run to completion before serving.**
Because the backfill is anti-join/resumable, an interrupted init self-heals on the next init; the spec states this dependency explicitly rather than relying on the straggler path to cover a missing word-table row.

**Precondition / fallback:** the zero-blob-read backfill holds only where a `raw_text` row is present.
Content whose `raw_text` is absent (never-indexed or binary content) cannot be rebuilt from stored text.
On its next search such content is a straggler, so the native search **fails loud** (`ReindexRequiredError`, path-scoped) for both REGEX and FULLTEXT — `reindex` rebuilds it from the blob (see "Native-search self-healing cull").

**Version bump (policy).**
The repository requires a version bump of the affected artifact for retrieval-scoring changes.
`SearchArtifact.provider_version` is bumped (from `"1"`, at the five construction sites: `sqlite_metadata.index_text`, `postgres_metadata.index_text`, and the three `vfs.py` artifact builders) **on new writes** as a forward marker.
Be honest about its limits: it is informational only (`is_usable` compares `params_hash`, never `provider_version`), and because existing records are _not_ rewritten, they keep `provider_version="1"` while serving the new word-token behavior — so the field does **not** distinguish old-vs-new behavior on the pre-existing records that the migration actually covers.
The load-bearing migration mechanism is the backfill; `provider_version` satisfies the policy's version-field requirement but is not what makes the migration correct.
`params_hash` is intentionally left unchanged for the reasons above.

**Rationale:** Because the searchable text is already stored content-addressed and the indexes are derived from it, the new representation is derivable in-place without re-keying or re-reading blobs.
The migration/test plan is the init-time backfill plus the post-migration correctness/equivalence scenarios.
SemVer: **MINOR** — additive public API; fulltext result-set behavior changes from trigram-substring to word-token semantics, a deliberate documented correctness fix (pre-1.0, so result-set changes are permitted in a MINOR bump).

### Decision: Boundary term-count cap

**Chosen:** Enforce a maximum query-term count in `vfs.VFS.search` (covering both backends), raising a clear error above the cap.
ALL is a single bound parameter regardless of count, but ANY grows one SQL expression + bind parameter per term on both backends; the cap bounds parser/driver resource use (Postgres has a hard bind-parameter ceiling).

**Rationale:** Validate untrusted input at the external boundary (directive #1).
One check at the public method covers SQLite and Postgres uniformly.

### Decision: future `NativeTextSearch` implementations — additive, no dependency

**Chosen:** The `NativeTextSearch` protocol gains `match_mode` via `SearchRequest`; any future implementation honors it when it lands.
Because `match_mode` has a default, a future provider compiles unchanged and uses `ALL` until it branches explicitly.
No build-order dependency.

> The `object-store-text-index` change (a candidate such implementation) is **parked** as scope creep (2026-06-19); this note implies no pending dependency.

### Decision: Native-search self-healing cull — fresh is authoritative, any straggler fails loud

> The decisions in this block were validated by an adversarial review (provenance below) before
> the cull was adopted. They reduce `vfs._native_search` to classify + fail-loud + fresh-serve.

**Chosen:** Classify each in-scope version by its artifact and serve only from the index:

- **Decided** — identity-current artifact (`content_hash` and `params_hash` both match): a `ready`
  artifact answers from stored text; an `unsupported` or `failed` artifact is a confirmed
  non-match (binary or un-indexable content) and is excluded.
- **Straggler** — artifact absent or identity-drifted: the index cannot vouch for the version.

If any in-scope version is a straggler, raise `ReindexRequiredError` (naming a path-scoped `reindex`) — no blob reads, no approximation, no partial results, for **both** REGEX and FULLTEXT.
A capability store error raises `IndexUnavailableError`.
This **culls** the guarded-reader straggler verify loop, the query-time lazy backfill, the `has_text_artifacts` existence re-check, and the inline FULLTEXT token predicate.
The guarded reader and `max_content_reads` survive only on the brute-force fallback path (no native capability).

**Rationale:** Indexing is atomic with the version write, so a fresh index is the steady state and stragglers are a migration/index-build transient that `reindex` owns.
The REGEX bounded verify (phase 2's "graceful middle") drops to a UX courtesy the migration window doesn't need; the inline FULLTEXT predicate (Python token-containment) is additionally dishonest — not `unicode61`/`'simple'` word semantics, cannot honor `ALL`/`ANY` or ranking — so it returns results the fresh index would not, making "fresh fulltext is authoritative" a lie the moment a straggler appears.
Failing loud is the honest floor for both.
The classifier itself is the irreducible core: without it, an un-indexed version silently contributes no match and search lies "not found."
The culled mechanisms served no PoC user story; the kept floor serves **US-2** (trustworthy search).

**Consequence:** the whole-word-semantics (`FulltextMatchesWholeWordsNotSubstrings`) and
cross-backend-equivalence scenarios are specified over **fresh (`ready`) records**, so failing loud
on stale content **strengthens** those guarantees — stale content can no longer return a divergent
approximation.

**Adversarial review (provenance):** before culling, three independent reviewers (Opus / Sonnet / Haiku) were tasked to **break** the thesis "stragglers are only a migration transient."
All three converged on one real counterexample: **`copy` and `move` commit a current version with `search_meta = {}`** and never call `index_text`, so the destination is a straggler on every copy/move (the text record exists, content-addressed by the shared `content_hash`, but the manifest pointer is missing).
`rollback` does not have this defect — it already copies `search_meta`.
The finding does not reverse the cull; it adds a prerequisite (propagation, below).
Today's self-healing was in effect **masking** the defect by verifying+backfilling the destination at query cost; culling without the fix would convert that hidden cost into a hard failure, so the fix lands in this change.

### Decision: `copy` / `move` propagate `search_meta` (prerequisite)

**Chosen:** `vfs.copy` and `vfs.move` set `search_meta=src_version.search_meta` on the destination
`VersionMeta`, mirroring `rollback`.

**Rationale:** The text record is content-addressed by `content_hash`, which copy/move preserve, so the propagated artifact is identity-current and the destination is immediately fresh — zero reads, no reindex.
This makes the golden-path-atomic premise true and stragglers genuinely transient.
**Write-sites:** the destination `VersionMeta(...)` in `vfs.copy` and in `vfs.move`; each carries its own test.

### Decision: `failed` is a decided non-match, not a straggler

**Chosen:** an identity-current `failed` artifact joins `unsupported` as a confirmed non-match
(excluded), not a straggler.

**Rationale:** treating it as a straggler would fail loud forever on an oversized/un-indexable file, since `reindex` re-produces `failed`.
Excluding it is a documented PoC limitation (un-indexable text is absent from text search), not an error.

### Decision: `_blob_gc` reference-check→delete made atomic

**Chosen:** wrap the `has_version_references` check and `delete_text_artifacts` in one metadata transaction so a reviving write cannot delete a live-referenced hash's **text artifacts**.
The blob delete stays outside the transaction (a separate store); the cross-store revive race — a write reviving a `content_hash` between the metadata commit and `blob.delete`, briefly leaving a live version's content blob reclaimed — is inherent and pre-existing, accepted at PoC scale.

**Rationale:** the text-artifact atomicity is the **search**-correctness invariant the removed existence re-check guarded (search reads the text artifact); fixing it at the source beats a per-query existence query on the hot path.
The residual blob race is a **read**-correctness gap; closing it needs grace-period / generational blob GC — out of scope for the PoC, and the search path does not depend on it.

### Decision: Path-scoped `reindex` remediation

**Chosen:** the fail-loud error names a path-scoped `reindex` (the search scope), so the remedy is
the stale subtree, not necessarily the whole namespace.

**Rationale:** a namespace-wide `reindex` is O(files × blob); pointing fail-loud at the narrowest covering scope keeps the remedy proportionate.
The contract (stale → reindex) is scale-independent; the operational "cheap" assumption holds at bounded PoC scale and is not written into the spec.

## Architecture

```text
   session.search(query, scope, FULLTEXT, match_mode=ANY)
         │
         ▼
   vfs.search(..., match_mode=ANY)         ── validates max term count ──►
         │  builds SearchRequest(search_type=FULLTEXT, match_mode=ANY, ...)
         ▼
   NativeTextSearch.search_text(request, visible_version_ids)
         │
   REGEX ─┤  (trigram representation — unchanged)
         │     SQLite: search_fts MATCH '"literal"'   /   Postgres: raw_text ~ :pat (pg_trgm)
         │
   FULLTEXT
         ├─► SQLite: _fulltext_search(query, mode)   → WORD table (unicode61)
         │     ALL: "tok1" "tok2"     ANY: "tok1" OR "tok2"     ORDER BY bm25 rank
         │
         └─► Postgres: _fulltext_search(query, mode) → to_tsvector('simple', raw_text)
               ALL: plainto_tsquery('simple', :q)
               ANY: plainto_tsquery('simple', :t0) || plainto_tsquery('simple', :t1)
               ORDER BY ts_rank DESC

   Indexes derive from search_text_artifacts.raw_text (content-addressed, already stored):
     trigram FTS5  (regex)      word FTS5 / inline 'simple' tsvector  (fulltext)
   Migration: init-time backfill of the word index from raw_text — no params_hash bump, zero blob reads.
   match_mode applies only to FULLTEXT; GLOB/FIND/REGEX ignore it.
   Result-identity (content → visible occurrences) unchanged across modalities and modes.
```

## Risks

- **Second FTS5 index cost (SQLite):** `index_text` now writes the decoded text into two derived indexes (trigram + word), increasing write time and on-disk size.
  Accepted as the cost of correct, modality-appropriate representations.
- **Tokenizer non-identity (`unicode61` vs `'simple'`):** the two are _aligned_, not identical — diacritic folding and URL/email/host tokenization can differ.
  The equivalence guarantee is therefore scoped to _portable_ terms and the residual is documented, not asserted away.
- **ALL-mode behavior changes vs. today:** moving fulltext from trigram-substring to word tokens changes which documents match (e.g. `cat` no longer matches `category`; `s3` now matches correctly).
  This is a deliberate correctness fix, surfaced in the migration note and CHANGELOG, not a silent regression.
- **Weaker English recall (non-stemming):** `databases` no longer matches `database`.
  Accepted; a stemmed profile is a future opt-in.
- **Migration completeness:** content whose `raw_text` row is missing (never-indexed or binary content) cannot be rebuilt without a blob read.
  Such content is a straggler, so the native search fails loud (`ReindexRequiredError`) for both REGEX and FULLTEXT — `reindex` is the remedy in both cases.
- **Any straggler fails loud (no verification, no approximation):** a missing or identity-drifted artifact raises `reindex`-required for both REGEX and FULLTEXT rather than being verified via blob read or approximated inline (see "Native-search self-healing cull").
  This removes the former Python token-containment best-effort (which diverged from `unicode61` / `'simple'` word semantics and could not honor `ALL` / `ANY` or ranking) **and** the bounded REGEX verify.
  Whole-word semantics (`FulltextMatchesWholeWordsNotSubstrings`) and cross-backend equivalence remain specified over **fresh (`ready`) records**; `copy`/`move` now propagate `search_meta` so derived versions are not stragglers.
- **Injection (SQLite OR join):** `OR` is a bare ASCII literal between double-quoted tokens, never user-derived; identical surface to the existing AND join.
- **Future `NativeTextSearch` providers default to `ALL`** until they branch on `match_mode`: acceptable; the protocol docstring documents the obligation (`object-store-text-index`, the candidate provider, is parked).
- **`failed` content excluded from text search:** an oversized/un-indexable text file is silently absent from FULLTEXT/REGEX (decided non-match) rather than failing loud.
  Documented PoC limitation; revisit if oversized indexing is implemented.

## Verification Waivers

- **Requirement:** `NativeTextSearchStorage` GC live-reference invariant (`LiveReferencedContentNeverSwept`) on Postgres **Reason:** requires the Docker-compose Postgres stack; not runnable in the sandbox.
  Mongo exposes no `NativeTextSearch`, so it has no text artifacts to sweep — the invariant is N/A there, and its blob-GC behavior is unchanged by this change.
  **Manual evidence:** `tests/integration/test_postgres_metadata.py::TestPostgresNativeTextSearch::test_live_referenced_content_never_swept`, run by the user against the compose stack; result recorded in the change before sync.
  **Recorded:** 2026-06-20
