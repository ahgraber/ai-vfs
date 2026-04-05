"""BlobStore protocol definition."""

from __future__ import annotations

from typing import AsyncIterator, Protocol, runtime_checkable


@runtime_checkable
class BlobStore(Protocol):
    """Content-addressed blob storage."""

    async def put(self, content_hash: str, data: bytes) -> None:
        """Store bytes under the given content hash."""
        ...

    async def get(self, content_hash: str) -> bytes:
        """Retrieve bytes by content hash; raise if not found."""
        ...

    async def delete(self, content_hash: str) -> None:
        """Remove the blob for the given content hash."""
        ...

    async def exists(self, content_hash: str) -> bool:
        """Return True if a blob exists for the given content hash."""
        ...

    async def put_stream(self, content_hash: str, stream: AsyncIterator[bytes]) -> None:
        """Store a streaming blob under the given content hash."""
        ...

    async def get_stream(self, content_hash: str) -> AsyncIterator[bytes]:
        """Yield chunks of the blob for the given content hash."""
        ...

    def list_hashes(self) -> AsyncIterator[str]:
        """Yield all content hashes present in the store."""
        ...
