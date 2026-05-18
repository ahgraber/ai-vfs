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

| Decision            | Choice                                                      | Rationale                                                                                  |
| ------------------- | ----------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| Integration pattern | Composition (not inheritance)                               | Provider composes bloom-search functions; satisfies `SearchProvider` protocol structurally |
| Index storage       | Per-version `SearchArtifact` at `search_meta[provider_key]` | Atomic with writes; versioning for free; no separate index store                           |
| Search pipeline     | Provider owns full pipeline internally                      | VFS dispatch stays generic; no bloom-specific orchestration in core                        |
| Scope limiting      | CWD-expanding shells + recency                              | Agents search near where they work; graceful degradation for brute-force paths             |
| Protocol changes    | Use `SearchRequest` with `search_metas` + `read_content`    | Needed by any index-accelerated provider; backward-compatible for default provider         |

---

## 2. SearchProvider Protocol Changes

The existing protocol changes `search()` to accept a `SearchRequest`.
The request carries the permission-pruned metadata and controlled content reader that index-accelerated providers need:

```python
@dataclass(frozen=True)
class SearchArtifact:
    status: Literal["ready", "failed", "unsupported"]
    schema_version: int
    provider_key: str
    provider_version: str | None
    params_hash: str
    content_hash: str
    created_at: datetime

    storage: Literal["inline", "blob", "external"]
    payload: dict | None = None
    artifact_ref: str | None = None

    error_code: str | None = None
    error_message: str | None = None


SearchMeta = dict[str, SearchArtifact]


@dataclass(frozen=True)
class SearchMetaEntry:
    path: str
    file: FileMeta
    version: VersionMeta
    search_meta: SearchMeta


ContentReader = Callable[[str], Awaitable[bytes]]


@dataclass(frozen=True)
class SearchRequest:
    query: str
    scope: str
    search_type: SearchType
    search_metas: Iterable[SearchMetaEntry]
    read_content: ContentReader
    limits: SearchLimits


class SearchProvider(Protocol):
    def provider_key(self) -> str: ...

    async def index(self, path: str, content: bytes, version: VersionMeta) -> SearchArtifact | None:
        """Build a provider artifact from content."""
        ...

    async def search(self, request: SearchRequest) -> SearchResponse:
        """Search within scope. Provider owns the full pipeline."""
        ...

    def capabilities(self) -> set[SearchType]: ...
```

- **`request.search_metas`**: entries for all files in scope, already permission-pruned and resolved to current versions.
  The VFS queries metadata and passes results to the provider.
  Providers that don't use indexes ignore the dict contents.
- **`request.read_content`**: async callback to fetch file content by path.
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

    def provider_key(self) -> str:
        return f"bloom/{self._normalizer_id}"

    async def index(self, path: str, content: bytes, version: VersionMeta) -> SearchArtifact | None:
        """Build BloomIndex from content, return a standard artifact envelope."""
        text = content.decode("utf-8", errors="replace")
        idx = build_index(
            text,
            window=self._window,
            target_fpp=self._target_fpp,
            normalizer=get_normalizer(self._normalizer_id),
        )
        return SearchArtifact(
            status="ready",
            schema_version=SUPPORTED_SCHEMA_VERSION,
            provider_key=self.provider_key(),
            provider_version=PROVIDER_VERSION,
            params_hash=self._params_hash(),
            content_hash=version.content_hash,
            created_at=utcnow(),
            storage="inline",
            payload=BloomSearchMeta.from_index(idx).model_dump(),
        )

    async def search(self, request: SearchRequest) -> SearchResponse:
        """Full pipeline: plan → filter → read candidates → verify."""
        # Phase 1: Build query plan
        plan = build_query_plan(
            request.query,
            window=self._window,
            normalizer=get_normalizer(self._normalizer_id),
        )

        # Phase 2: Deserialize indexes and filter candidates
        indexed = []
        unindexed = []
        for entry in request.search_metas:
            artifact = entry.search_meta.get(self.provider_key())
            if artifact and self._can_use(artifact, entry.version):
                idx = BloomSearchMeta.model_validate(artifact.payload).to_index()
                indexed.append((entry.path, idx))
            else:
                unindexed.append(entry.path)

        candidates = filter_candidates(indexed, plan, strict=self._strict)
        # Unindexed files are always candidates (conservative)
        candidates.extend(unindexed)

        # Phase 3: Read content and verify matches
        results = []
        for path in candidates:
            content = await request.read_content(path)
            matches = self._verify(request.query, request.search_type, content)
            if matches:
                results.append(SearchResult(path=path, matches=matches))

        return SearchResponse(results=results)

    def capabilities(self) -> set[SearchType]:
        return {SearchType.regex, SearchType.fulltext}

    def _can_use(self, artifact: SearchArtifact, version: VersionMeta) -> bool:
        """Check whether the artifact envelope matches this provider and version."""
        return (
            artifact.status == "ready"
            and artifact.schema_version <= SUPPORTED_SCHEMA_VERSION
            and artifact.params_hash == self._params_hash()
            and artifact.content_hash == version.content_hash
            and artifact.payload is not None
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
  ├─ 4. provider.search(SearchRequest(query, scope, type, search_metas, read_content, limits))
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
  └─ 5. Return SearchResponse
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
    scope_narrowed: bool = False  # true if brute-force scope was limited
    actual_scope: str | None = None  # the scope that was actually searched
    total_files_in_scope: int | None = None  # full count before narrowing
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
  → provider.index(path, content, version)
  → store SearchArtifact at version.search_meta[provider.provider_key()]
```

Inline during every write.
Cost: one pass over content to build bloom filter + masks.
Sub-millisecond for typical document sizes.

### 6.2 Lazy Backfill (On Search Miss)

```text
vfs.search(query, scope)
  → provider receives search_metas for files in scope
  → files missing the provider key pass through as candidates (conservative)
  → VFS reads content for verification
  → VFS optionally backfills SearchArtifact entries for those files
```

The provider never triggers backfill — it returns unindexed files as candidates.
The VFS decides whether to backfill opportunistically.

### 6.3 Batch Reindex (Explicit)

```text
vfs.reindex(namespace, provider="bloom", scope="/src/")
  → iterate all current versions in scope
  → read content, call provider.index() for each
  → update search_meta[provider.provider_key()] on each version
```

Used when: provider config changes, bloom-search upgrades change the normalizer
fingerprint, or initial onboarding of a large existing namespace.

### 6.4 Versioning Implications

Search metadata is **per-version**, not per-file:

- Rollback to version 3 creates version N+1 with the same `content_hash`.
  The `search_meta` can be copied from version 3 — no reindex needed.
- Each version is self-contained: every ready artifact records the matching
  `content_hash`.
- GC reclaims versions and their search_meta together — no orphaned indexes.

---

## 7. Error Handling

The principle: **never fail a search, only degrade to brute-force.**
Index problems reduce performance, not correctness.
Content verification (regex match against actual content) is always the final arbiter.

| Scenario                                      | Who detects                       | Behavior                                                                                  |
| --------------------------------------------- | --------------------------------- | ----------------------------------------------------------------------------------------- |
| Params hash mismatch                          | Provider (`search`)               | Treat as unindexed; file passes through as candidate.                                     |
| Missing provider key                          | Provider (`search`)               | File passes through as candidate. VFS may backfill.                                       |
| Unknown artifact `schema_version`             | Provider (`search`)               | Treat as unindexed.                                                                       |
| Artifact `content_hash` differs from version  | Provider (`search`)               | Treat as stale and unindexed.                                                             |
| `EmptyPlan` (no extractable n-grams)          | bloom-search (`build_query_plan`) | Provider returns all files as candidates. Scope limiting applies.                         |
| Content decode failure during index           | Provider (`index`)                | Return a `failed` or `unsupported` artifact. File remains searchable through brute force. |
| `strict=False` with stale provider parameters | Provider config                   | Filters using stale index. May produce false negatives. Caller accepted risk.             |

---

## 8. Storage Cost

Per-file bloom payload stored inside a `SearchArtifact` envelope:

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
