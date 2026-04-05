"""SearchProvider protocol definition."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from vfs.models import FileMeta, SearchResult, SearchType


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
    ) -> list[SearchResult]:
        """Execute a search query within the given scope and return ranked results."""
        ...

    def capabilities(self) -> set[SearchType]:
        """Return the set of search types this provider supports."""
        ...
