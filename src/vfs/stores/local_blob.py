"""Local filesystem blob store with prefix directory structure."""

from __future__ import annotations

from pathlib import Path
from typing import AsyncIterator

import aiofiles

from vfs.errors import NotFoundError


class LocalFSBlobStore:
    """Content-addressed blob storage on the local filesystem.

    Blobs are stored at ``{base_path}/{hash[0:2]}/{hash[2:4]}/{hash}``.
    """

    def __init__(self, base_path: str | Path) -> None:
        self._base = Path(base_path)

    def _path(self, content_hash: str) -> Path:
        return self._base / content_hash[0:2] / content_hash[2:4] / content_hash

    async def put(self, content_hash: str, data: bytes) -> None:
        """Write data to disk; no-op if the blob already exists."""
        p = self._path(content_hash)
        if p.exists():
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        async with aiofiles.open(p, "wb") as f:
            await f.write(data)

    async def get(self, content_hash: str) -> bytes:
        """Return the full blob content. Raises NotFoundError if absent."""
        p = self._path(content_hash)
        if not p.exists():
            raise NotFoundError(f"blob {content_hash!r} not found")
        async with aiofiles.open(p, "rb") as f:
            return await f.read()

    async def delete(self, content_hash: str) -> None:
        """Remove the blob file; no-op if it does not exist."""
        self._path(content_hash).unlink(missing_ok=True)

    async def exists(self, content_hash: str) -> bool:
        """Return True if the blob file is present on disk."""
        return self._path(content_hash).exists()

    async def put_stream(self, content_hash: str, stream: AsyncIterator[bytes]) -> None:
        """Not implemented for the local filesystem store."""
        raise NotImplementedError

    async def get_stream(self, content_hash: str) -> AsyncIterator[bytes]:
        """Not implemented for the local filesystem store."""
        raise NotImplementedError
        yield b""  # make this an async generator  # pragma: no cover

    async def list_hashes(self) -> AsyncIterator[str]:
        """Enumerate all stored content hashes."""
        if not self._base.exists():
            return
        for prefix1 in sorted(self._base.iterdir()):
            if not prefix1.is_dir():
                continue
            for prefix2 in sorted(prefix1.iterdir()):
                if not prefix2.is_dir():
                    continue
                for blob_file in sorted(prefix2.iterdir()):
                    if blob_file.is_file():
                        yield blob_file.name
