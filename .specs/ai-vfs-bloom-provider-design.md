# ai-vfs: Bloom Search Provider Integration

**Date:** 2026-04-04 **Status:** Draft **Authors:** ahgraber + Claude

**Inspired by:** [Cursor: Fast Regex Search](https://cursor.com/blog/fast-regex-search) — trigram indexing with bloom filters and augmented masks for code search acceleration.
Adapted here for generic document content in a virtual filesystem context.

---

## 1. Overview

This document specifies how ai-vfs integrates ahgraber/bloom-search as an index-accelerated
`SearchProvider` for regex and full-text search. bloom-search is a pure library —
ai-vfs imports its functions and types, wraps them in a `SearchProvider` implementation,
and orchestrates the index-then-filter pipeline.

### Goals

- `BloomSearchProvider` that satisfies the `SearchProvider` protocol via composition
- Two-phase search: bloom filter candidates (coarse) → content verification (fine)
- Scope limiting for brute-force fallback paths (CWD-expanding heuristic)
- Seamless indexing lifecycle: on-write, lazy backfill, batch reindex
- No bloom-specific logic in VFS core — all behind the provider interface

### Non-Goals

- Modifying bloom-search — all extensions are specified in the companion doc
- Global/aggregate indexes — per-file bloom filters in `search_meta` are sufficient at target scale
- Semantic search integration — separate provider, separate spec

### Design Decisions

| Decision            | Choice                                                           | Rationale                                                                                  |
| ------------------- | ---------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| Integration pattern | Composition (not inheritance)                                    | Provider composes bloom-search functions; satisfies `SearchProvider` protocol structurally |
| Index storage       | Per-version in `search_meta["bloom"]`                            | Atomic with writes; versioning for free; no separate index store                           |
| Search pipeline     | Provider owns full pipeline internally                           | VFS dispatch stays generic; no bloom-specific orchestration in core                        |
| Scope limiting      | CWD-expanding shells + recency                                   | Agents search near where they work; graceful degradation for brute-force paths             |
| Protocol changes    | Add `search_metas` + `read_content` to `SearchProvider.search()` | Needed by any index-accelerated provider; backward-compatible for default provider         |

---

## 2. SearchProvider Protocol Changes

The existing protocol gains two parameters on `search()`:

```python
class SearchProvider(Protocol):
    async def index(self, path: str, content: bytes, metadata: FileMeta) -> dict:
        """Build search artifacts from content. Returns dict for search_meta."""
        ...

    async def search(
        self,
        query: str,
        scope: str,
        search_type: SearchType,
        search_metas: Iterable[tuple[str, dict]],  # NEW
        read_content: Callable[[str], Awaitable[bytes]],  # NEW
    ) -> list[SearchResult]:
        """Search within scope. Provider owns the full pipeline."""
        ...

    def capabilities(self) -> set[SearchType]: ...
```

- **`search_metas`**: `(path, search_meta_dict)` pairs for all files in scope, already permission-pruned and resolved to current versions.
  The VFS queries metadata and passes results to the provider.
  Providers that don't use indexes ignore the dict contents.
- **`read_content`**: async callback to fetch file content by path.
  Providers call this only for candidates that need content verification.
  The VFS owns blob access; the provider never touches storage directly.

The default provider (glob/grep) ignores `search_metas` contents and calls
`read_content` for every file — same brute-force behavior, same interface.

---

## 3. BloomSearchProvider

### 3.1 Implementation

```python
from bloom import build_index, build_query_plan, filter_candidates, BloomSearchMeta


class BloomSearchProvider:
    """SearchProvider backed by bloom-search. Composes library functions."""

    def __init__(
        self,
        *,
        window: int = 3,
        target_fpp: float = 0.015,
        normalizer_id: str = "default",
        strict: bool = True,
    ):
        self._window = window
        self._target_fpp = target_fpp
        self._normalizer_id = normalizer_id
        self._strict = strict

    async def index(self, path: str, content: bytes, metadata: FileMeta) -> dict:
        """Build BloomIndex from content, return serialized as dict."""
        text = content.decode("utf-8", errors="replace")
        idx = build_index(
            text,
            window=self._window,
            target_fpp=self._target_fpp,
            normalizer=get_normalizer(self._normalizer_id),
        )
        return {"bloom": BloomSearchMeta.from_index(idx).model_dump()}

    async def search(
        self,
        query: str,
        scope: str,
        search_type: SearchType,
        search_metas: Iterable[tuple[str, dict]],
        read_content: Callable[[str], Awaitable[bytes]],
    ) -> list[SearchResult]:
        """Full pipeline: plan → filter → read candidates → verify."""
        # Phase 1: Build query plan
        plan = build_query_plan(
            query,
            window=self._window,
            normalizer=get_normalizer(self._normalizer_id),
        )

        # Phase 2: Deserialize indexes and filter candidates
        indexed = []
        unindexed = []
        for path, meta_dict in search_metas:
            bloom_data = meta_dict.get("bloom")
            if bloom_data and self._can_use(bloom_data):
                idx = BloomSearchMeta.model_validate(bloom_data).to_index()
                indexed.append((path, idx))
            else:
                unindexed.append(path)

        candidates = filter_candidates(indexed, plan, strict=self._strict)
        # Unindexed files are always candidates (conservative)
        candidates.extend(unindexed)

        # Phase 3: Read content and verify matches
        results = []
        for path in candidates:
            content = await read_content(path)
            matches = self._verify(query, search_type, content)
            if matches:
                results.append(SearchResult(path=path, matches=matches))

        return results

    def capabilities(self) -> set[SearchType]:
        return {SearchType.regex, SearchType.fulltext}

    def _can_use(self, bloom_data: dict) -> bool:
        """Check if bloom_data has a known schema version and matching normalizer_id."""
        return (
            bloom_data.get("version", 0) <= SUPPORTED_SCHEMA_VERSION
            and bloom_data.get("normalizer_id") == self._normalizer_id
        )

    def _verify(self, query: str, search_type: SearchType, content: bytes) -> list[Match] | None:
        """Run actual regex/fulltext match against content. Returns matches or None."""
        ...
```

### 3.2 Registration

```python
vfs = VFS(
    search_providers=["bloom"],  # resolved to BloomSearchProvider
)
```

Or explicit construction:

```python
provider = BloomSearchProvider(normalizer_id="default", strict=True)
vfs = VFS(search_providers=[provider])
```

---

## 4. Search Dispatch Flow

```text
vfs.search("error.*timeout", scope="/src/", type=regex)
  │
  ├─ 1. Resolve scope: list files in /src/ (metadata only, permission-pruned)
  │
  ├─ 2. Find provider: which provider declares regex capability? → BloomSearchProvider
  │
  ├─ 3. Query search_meta for all files in scope (metadata batch read)
  │
  ├─ 4. provider.search(query, scope, type, search_metas, read_content)
  │     │
  │     ├─ 4a. build_query_plan("error.*timeout") → AndNode(["err","rro","ror"], ["tim","ime","meo","eou","out"])
  │     │
  │     ├─ 4b. filter_candidates(indexed_files, plan) → candidate paths
  │     │       (bloom filter eliminates most files — metadata only, no blob reads)
  │     │
  │     ├─ 4c. Add unindexed files to candidates (conservative)
  │     │
  │     └─ 4d. For each candidate: read_content(path) → regex verify → results
  │
  └─ 5. Return SearchResult list
```

Steps 1-3 are metadata-only.
The bloom filter at step 4b eliminates most files.
Only candidates hit the blob store at step 4d.

---

## 5. Scope Limiting for Brute-Force Fallback

When the bloom provider returns `EmptyPlan` (no extractable n-grams) or when files are unindexed, the search degrades to brute-force: read every file and match.
At scale-B file counts (tens of thousands) against blob storage, this is too expensive.

### 5.1 Strategy

The VFS enforces a `search_brute_force_limit` on the number of files that can be brute-forced.
When the candidate set after bloom filtering exceeds this:

1. Start with the agent's `$CWD`
2. Expand outward in shells: `$CWD`, `$CWD/..`, `$CWD/../..`, ...
3. Stop when the original scope is reached or the limit is hit
4. If even `$CWD` alone exceeds the limit, sort by recency (most recently modified) and truncate

### 5.2 Scope Limiting Applies After Bloom Filtering

```text
Resolve scope → 10K files
  │
  ├─ Query plan exists (not EmptyPlan):
  │     bloom filter → ~100 candidates → read & verify → done (no scope limit needed)
  │
  └─ EmptyPlan or unindexed files:
        apply CWD-expanding scope limit → narrow to 500 files → read & verify
        flag scope_narrowed=true in response
```

Bloom-indexed queries search the full scope cheaply (metadata-only filtering).
Scope limiting only kicks in for the brute-force path.

### 5.3 SearchResult Metadata

```python
class SearchResponse:
    results: list[SearchResult]
    scope_narrowed: bool  # true if brute-force scope was limited
    actual_scope: str  # the scope that was actually searched
    total_files_in_scope: int  # full count before narrowing
```

This lets the agent know results may be partial and decide whether to refine.

### 5.4 Configuration

```python
class VFSConfig(BaseSettings):
    ...
    search_brute_force_limit: int = 500  # max files for unindexed/EmptyPlan search
```

---

## 6. Indexing Lifecycle

### 6.1 On Write (Synchronous)

```text
vfs.write(path, content)
  → provider.index(path, content, metadata)
  → store result in version.search_meta["bloom"]
```

Inline during every write.
Cost: one pass over content to build bloom filter + masks.
Sub-millisecond for typical document sizes.

### 6.2 Lazy Backfill (On Search Miss)

```text
vfs.search(query, scope)
  → provider receives search_metas for files in scope
  → files missing "bloom" key pass through as candidates (conservative)
  → VFS reads content for verification
  → VFS optionally backfills search_meta for those files
```

The provider never triggers backfill — it returns unindexed files as candidates.
The VFS decides whether to backfill opportunistically.

### 6.3 Batch Reindex (Explicit)

```text
vfs.reindex(namespace, provider="bloom", scope="/src/")
  → iterate all current versions in scope
  → read content, call provider.index() for each
  → update search_meta on each version
```

Used when: provider config changes, bloom-search upgrades change the normalizer
fingerprint, or initial onboarding of a large existing namespace.

### 6.4 Versioning Implications

Search metadata is **per-version**, not per-file:

- Rollback to version 3 creates version N+1 with the same `content_hash`.
  The `search_meta` can be copied from version 3 — no reindex needed.
- Each version is self-contained: its search_meta matches its content.
- GC reclaims versions and their search_meta together — no orphaned indexes.

---

## 7. Error Handling

The principle: **never fail a search, only degrade to brute-force.**
Index problems reduce performance, not correctness.
Content verification (regex match against actual content) is always the final arbiter.

| Scenario                                 | Who detects                        | Behavior                                                                                                          |
| ---------------------------------------- | ---------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| Normalizer fingerprint mismatch          | bloom-search (`filter_candidates`) | Raises `NormalizerDriftError`. Provider catches, returns mismatched files as unfiltered candidates. Logs warning. |
| `search_meta` missing `"bloom"` key      | Provider (`search`)                | File passes through as candidate. VFS may backfill.                                                               |
| `search_meta` has unknown `version`      | Provider (`search`)                | Treat as unindexed — forward-compatible.                                                                          |
| `EmptyPlan` (no extractable n-grams)     | bloom-search (`build_query_plan`)  | Provider returns all files as candidates. Scope limiting applies.                                                 |
| Content decode failure during index      | Provider (`index`)                 | Return empty dict. File is searchable but unindexed. Log warning.                                                 |
| `strict=False` with fingerprint mismatch | bloom-search                       | Filters using stale index. May produce false negatives. Caller accepted risk.                                     |

---

## 8. Storage Cost

Per-file bloom index stored in `search_meta["bloom"]`:

| Component          | Size       |
| ------------------ | ---------- |
| Base bloom filter  | ~500 bytes |
| next_masks         | ~2KB       |
| loc_masks          | ~2KB       |
| Metadata overhead  | ~200 bytes |
| **Total per file** | **~5KB**   |

At 10,000 files per namespace (upper end of target scale): ~50MB of search metadata.
Fits comfortably in SQLite JSONB or Postgres JSONB.

---

## 9. Dependencies

ai-vfs adds bloom-search as an optional dependency:

```toml
[project.optional-dependencies]
bloom = ["bloom-search>=X.Y.Z"]
```

The `BloomSearchProvider` is only importable when the optional dependency is installed.
The VFS core has no dependency on bloom-search.
