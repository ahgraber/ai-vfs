"""Tests for LocalFSBlobStore."""

from __future__ import annotations

import pytest
import pytest_asyncio

from vfs.errors import NotFoundError
from vfs.protocols.blob import BlobStore
from vfs.stores.local_blob import LocalFSBlobStore


@pytest_asyncio.fixture
async def store(tmp_path):
    return LocalFSBlobStore(tmp_path / "blobs")


class TestLocalFSBlobStore:
    @pytest.mark.asyncio
    async def test_put_and_get(self, store):
        data = b"hello world"
        content_hash = "abcdef1234567890abcdef1234567890"
        await store.put(content_hash, data)
        result = await store.get(content_hash)
        assert result == data

    @pytest.mark.asyncio
    async def test_put_idempotent(self, store):
        data = b"hello world"
        content_hash = "abcdef1234567890abcdef1234567890"
        await store.put(content_hash, data)
        await store.put(content_hash, data)  # no error
        result = await store.get(content_hash)
        assert result == data

    @pytest.mark.asyncio
    async def test_exists(self, store):
        content_hash = "abcdef1234567890abcdef1234567890"
        assert await store.exists(content_hash) is False
        await store.put(content_hash, b"data")
        assert await store.exists(content_hash) is True

    @pytest.mark.asyncio
    async def test_delete(self, store):
        content_hash = "abcdef1234567890abcdef1234567890"
        await store.put(content_hash, b"data")
        await store.delete(content_hash)
        with pytest.raises(NotFoundError):
            await store.get(content_hash)

    @pytest.mark.asyncio
    async def test_prefix_directory_structure(self, store):
        content_hash = "abcdef1234567890abcdef1234567890"
        await store.put(content_hash, b"data")
        expected = store._base / "ab" / "cd" / content_hash
        assert expected.exists()

    @pytest.mark.asyncio
    async def test_put_stream_raises(self, store):
        with pytest.raises(NotImplementedError):
            await store.put_stream("hash", None)

    @pytest.mark.asyncio
    async def test_get_stream_raises(self, store):
        with pytest.raises(NotImplementedError):
            async for _ in store.get_stream("hash"):
                pass

    def test_conforms_to_protocol(self, store):
        assert isinstance(store, BlobStore)

    @pytest.mark.asyncio
    async def test_list_hashes(self, store):
        hashes = {"aa112233445566778899aabbccddeeff", "bb112233445566778899aabbccddeeff"}
        for h in hashes:
            await store.put(h, b"data")
        found = set()
        async for h in store.list_hashes():
            found.add(h)
        assert found == hashes
