"""Caching blob store wrapper using diskcache."""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

from diskcache import Cache

from vfs.protocols.blob import BlobStore


class CachedBlobStore:
    """LRU cache wrapper around any BlobStore implementation."""

    def __init__(
        self,
        inner: BlobStore,
        cache_dir: str,
        max_size_mb: int = 1024,
    ) -> None:
        self._inner = inner
        self._cache = Cache(
            cache_dir,
            size_limit=max_size_mb * 1024 * 1024,
            eviction_policy="least-recently-used",
        )

    async def put(self, content_hash: str, data: bytes) -> None:
        """Write through to both the inner store and the LRU cache."""
        await self._inner.put(content_hash, data)
        await asyncio.to_thread(self._cache.set, content_hash, data)

    async def get(self, content_hash: str) -> bytes:
        """Return blob content from cache if present, otherwise fetch from inner store and populate cache."""
        hit = await asyncio.to_thread(self._cache.get, content_hash)
        if hit is not None:
            return hit
        data = await self._inner.get(content_hash)
        await asyncio.to_thread(self._cache.set, content_hash, data)
        return data

    async def delete(self, content_hash: str) -> None:
        """Remove from both the inner store and the cache."""
        await self._inner.delete(content_hash)
        await asyncio.to_thread(self._cache.delete, content_hash)

    async def exists(self, content_hash: str) -> bool:
        """Return True if the blob is in cache or in the inner store."""
        hit = await asyncio.to_thread(self._cache.get, content_hash)
        if hit is not None:
            return True
        return await self._inner.exists(content_hash)

    async def put_stream(self, content_hash: str, stream: AsyncIterator[bytes]) -> None:
        """Not implemented."""
        raise NotImplementedError

    async def get_stream(self, content_hash: str) -> AsyncIterator[bytes]:
        """Not implemented."""
        raise NotImplementedError
        yield b""  # pragma: no cover

    async def list_hashes(self) -> AsyncIterator[str]:
        """Delegate enumeration to the inner store."""
        async for h in self._inner.list_hashes():
            yield h

    def close(self) -> None:
        """Close and release the diskcache."""
        self._cache.close()
