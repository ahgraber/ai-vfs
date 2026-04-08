"""SearchProvider protocol definition."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

from vfs.models import FileMeta, SearchResult, SearchType

#: Callback to lazily fetch file content by path.
#: The VFS closes over namespace + blob store so providers stay decoupled from storage.
ContentFetcher = Callable[[str], Awaitable[bytes]]


@runtime_checkable
class SearchProvider(Protocol):
    """Pluggable search backend."""

    async def index(self, path: str, content: bytes, metadata: FileMeta) -> dict:
        """Index content and metadata for a file; return provider-specific index metadata."""
        ...

    async def search(
        self,
        query: str,
        scope: str,
        search_type: SearchType,
        candidates: list[FileMeta] | None = None,
        fetch_content: ContentFetcher | None = None,
    ) -> list[SearchResult]:
        """Execute a search query within the given scope and return ranked results.

        ``fetch_content``, when provided, is an async callable that returns the
        blob bytes for a given path.  Providers that need file content (e.g.
        regex grep) call it on demand; metadata-only strategies ignore it.
        """
        ...

    def capabilities(self) -> set[SearchType]:
        """Return the set of search types this provider supports."""
        ...
