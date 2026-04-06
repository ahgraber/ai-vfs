"""Tests for CachedBlobStore."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from vfs.protocols.blob import BlobStore
from vfs.stores.cached_blob import CachedBlobStore


@pytest_asyncio.fixture
async def inner_store():
    mock = AsyncMock()
    mock.put = AsyncMock()
    mock.get = AsyncMock(return_value=b"data from inner")
    mock.delete = AsyncMock()
    mock.exists = AsyncMock(return_value=True)
    return mock


@pytest_asyncio.fixture
async def cached_store(inner_store, tmp_path):
    store = CachedBlobStore(inner_store, str(tmp_path / "cache"), max_size_mb=10)
    yield store
    store.close()


class TestCachedBlobStore:
    @pytest.mark.asyncio
    async def test_cache_miss_fetches_from_inner(self, cached_store, inner_store):
        result = await cached_store.get("hash1")
        assert result == b"data from inner"
        inner_store.get.assert_awaited_once_with("hash1")

    @pytest.mark.asyncio
    async def test_cache_hit_skips_inner(self, cached_store, inner_store):
        # Prime the cache
        await cached_store.put("hash1", b"cached data")
        inner_store.get.reset_mock()
        # Read should come from cache
        result = await cached_store.get("hash1")
        assert result == b"cached data"
        inner_store.get.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_write_through(self, cached_store, inner_store):
        await cached_store.put("hash1", b"data")
        inner_store.put.assert_awaited_once_with("hash1", b"data")
        # Cache should also have it
        inner_store.get.reset_mock()
        result = await cached_store.get("hash1")
        assert result == b"data"
        inner_store.get.assert_not_awaited()

    def test_conforms_to_protocol(self, cached_store):
        assert isinstance(cached_store, BlobStore)

    @pytest.mark.asyncio
    async def test_diskcache_wraps_in_thread(self, inner_store, tmp_path):
        """Ensure diskcache operations go through asyncio.to_thread."""
        store = CachedBlobStore(inner_store, str(tmp_path / "cache2"), max_size_mb=10)
        with patch("vfs.stores.cached_blob.asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread:
            mock_to_thread.return_value = b"thread data"
            await store.get("hash1")
            # to_thread should have been called for cache.get
            assert mock_to_thread.await_count >= 1
        store.close()
