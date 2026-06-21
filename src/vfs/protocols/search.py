"""SearchProvider protocol and supporting request/response types."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from vfs.models import FileMeta, FullTextMatchMode, SearchArtifact, SearchResult, SearchType

if TYPE_CHECKING:
    from vfs.search.reader import ContentReader


@dataclass(frozen=True)
class FindPredicates:
    """Typed predicates for FIND searches; all fields optional, combined conjunctively.

    ``type`` accepts ``"file"`` (live, non-tombstone), ``"tombstone"``, or ``None``
    (any).  Conjunctive matching logic is applied by the search provider.
    """

    name: str | None = None
    size_min: int | None = None
    size_max: int | None = None
    mtime_after: datetime | None = None
    mtime_before: datetime | None = None
    type: str | None = None


@dataclass(frozen=True)
class SearchMetaEntry:
    """Snapshot of one visible file version for a single search request.

    Carries the information the search provider and the guarded reader need:
    ``content_hash`` for blob retrieval by immutable hash; ``size`` and
    ``updated_at`` for predicate evaluation; ``search_meta`` for artifact
    usability checks (keyed by provider key, values are :class:`SearchArtifact`).
    """

    version_id: str
    path: str
    content_hash: str
    size: int
    updated_at: datetime
    is_deleted: bool = False
    search_meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SearchLimits:
    """Operational limits for a single search request."""

    max_content_reads: int = 10


@dataclass
class SearchRequest:
    """Bundled inputs for a :class:`SearchProvider` search call.

    ``search_metas`` is the permission-pruned set of current-version entries for
    files in scope.  ``read_content`` is the :class:`~vfs.search.reader.ContentReader`
    the VFS constructs for straggler verification — it resolves paths to their
    enumerated ``content_hash`` and enforces the ``limits.max_content_reads`` ceiling.
    """

    query: str
    scope: str
    search_type: SearchType
    search_metas: list[SearchMetaEntry]
    read_content: ContentReader  # type: ignore[type-arg]
    limits: SearchLimits = field(default_factory=SearchLimits)
    find_predicates: FindPredicates | None = None
    # Applies only to FULLTEXT searches; ignored for GLOB, FIND, and REGEX.
    match_mode: FullTextMatchMode = FullTextMatchMode.ALL


@dataclass
class SearchResponse:
    """Result of a :class:`SearchProvider` search call."""

    results: list[SearchResult] = field(default_factory=list)


@runtime_checkable
class NativeTextSearch(Protocol):
    """Optional metadata-store capability for accelerated text indexing and search.

    Stores that expose this capability return it from their ``native_text_search()``
    accessor; stores without it return ``None``.  The VFS dispatches regex/fulltext
    searches through this capability when present.

    Identity contract
    -----------------
    Searchable text is keyed by ``(provider_key, params_hash, content_hash)`` — mirroring
    how blobs key bytes by ``content_hash``:

    - ``content_hash``                                → bytes  (blob: document content)
    - ``(provider_key, params_hash, content_hash)``   → decoded text + status  (search: document text)
    - ``version_id``                                   → path, version_number, content_hash  (occurrence)

    A file *version* is an *occurrence* of content at a path; the searchable text is a
    property of the content, not the version.  ``params_hash`` in the key lets a
    tokenizer/extractor change produce a new record without clobbering the old, and lets a
    profile be retired by sweeping its ``params_hash``.

    Content→occurrence expansion
    ----------------------------
    ``search_text`` matches content (via stored raw text, no blob reads for fresh
    artifacts), then expands each match through the ``visible_version_ids`` that reference
    that content, emitting one ``SearchResult`` per visible occurrence.  Result identity
    always comes from the VFS-enumerated visible version (path, version number); the text
    record's stored fields are never used as result identity.

    Attributes
    ----------
    provider_key : str
        Stable string identifying this capability implementation (e.g. ``"vfs.sqlite_fts5"``).
        Used as the key in the version ``search_meta`` manifest.
    params_hash : str
        Short hex digest covering the tokenizer/extractor configuration.  A change to the
        config produces a new ``params_hash``, allowing the old records to be GC'd via the
        retired-params_hash sweep without clobbering new ones.
    """

    provider_key: str
    params_hash: str

    async def index_text(
        self,
        version_id: str,
        content_hash: str,
        params_hash: str,
        text: str,
    ) -> "SearchArtifact":
        """Index decoded text for a file version; called inside the version's write transaction.

        Upserts a content-addressed text record keyed by
        ``(provider_key, params_hash, content_hash)`` and returns a ``ready``
        ``external`` ``SearchArtifact`` referencing that record.  Content-level errors
        (undecodable, oversized) produce a ``failed``/``unsupported`` artifact in the same
        transaction (the write still succeeds); infrastructure errors abort the transaction.
        """
        ...

    async def search_text(
        self,
        request: "SearchRequest",
        visible_version_ids: list[str],
    ) -> "SearchResponse":
        """Match content and expand results to visible occurrences.

        Verifies matches against stored raw text (no blob reads for fresh artifacts), then
        joins each match through ``visible_version_ids`` to emit one result per visible
        occurrence with the occurrence's path and version number.
        """
        ...

    async def delete_text_artifacts(
        self,
        content_hashes: list[str],
        retired_params_hashes: list[str],
    ) -> None:
        """Delete text artifacts for orphaned content hashes or retired params-hash profiles.

        Called by the GC sweep: ``content_hashes`` are hashes with no remaining version
        references; ``retired_params_hashes`` are profiles whose params have changed.
        """
        ...


@runtime_checkable
class SearchProvider(Protocol):
    """Pluggable search backend."""

    async def index(self, path: str, content: bytes, metadata: FileMeta) -> SearchArtifact | None:
        """Index content and metadata for a file version.

        Returns a :class:`~vfs.models.SearchArtifact` when the provider produced an
        index artifact (e.g. native full-text search), or ``None`` when the provider
        performs no indexing (the default provider).
        """
        ...

    async def search(self, request: SearchRequest) -> SearchResponse:
        """Execute a search query and return a :class:`SearchResponse`.

        ``request.search_metas`` is the permission-pruned entry set.
        ``request.read_content`` provides content for straggler verification.
        Metadata-only providers (glob, find) never call ``read_content``.
        """
        ...

    def capabilities(self) -> set[SearchType]:
        """Return the set of search types this provider supports."""
        ...
