"""S3-backed blob store, implemented with aiobotocore's async client.

Importable only when the ``s3`` extra (``aiobotocore``) is installed; the URI resolver
guards the import and raises an actionable error otherwise. ``aiobotocore`` is imported
at module scope, so this module must not be imported unless the driver is present.

Mirrors the observable behavior of :class:`~vfs.stores.local_blob.LocalFSBlobStore`:

* Blobs are keyed under a sharded ``{prefix}/{hash[0:2]}/{hash[2:4]}/{hash}`` layout
  (or ``{hash[0:2]}/{hash[2:4]}/{hash}`` when ``prefix`` is empty).
* ``put`` is idempotent: a ``head_object`` short-circuits when the key already exists.
* ``get`` raises :class:`vfs.errors.NotFoundError` for missing keys.
* ``delete`` is a no-op when the key is absent (S3 ``delete_object`` is idempotent).
* ``put_stream`` / ``get_stream`` raise :class:`NotImplementedError`.
* ``list_hashes`` paginates the configured prefix and yields content hashes only.

Connection model: an :class:`aiobotocore.session.AioSession` client is **lazily** opened
on first use via :meth:`_ensure_client` and held for the store's lifetime, released by
:meth:`close`. Construction opens no connection — the URI-resolution unit test asserts
``self._client is None`` at construction. The aiobotocore client is an async context
manager whose ``__aexit__`` releases the underlying ``aiohttp`` connection pool; we
capture it on an :class:`contextlib.AsyncExitStack` so :meth:`close` can release it
deterministically.

Credentials, region, and endpoint URL come from the standard botocore environment
(``AWS_ACCESS_KEY_ID``, ``AWS_SECRET_ACCESS_KEY``, ``AWS_REGION``,
``AWS_ENDPOINT_URL_S3`` for MinIO/test setups, and the rest of the botocore config
chain). This store does not parse any custom env vars or accept overrides — callers
choose region/endpoint via the environment, matching botocore's documented contract.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from typing import Any, AsyncIterator
from urllib.parse import urlsplit

from aiobotocore.session import get_session
from botocore.exceptions import ClientError

from vfs.errors import NotFoundError

#: ClientError ``Error.Code`` values that mean "object not present".
#: ``head_object`` returns ``404`` / ``NotFound``; ``get_object`` returns ``NoSuchKey``.
_NOT_FOUND_CODES = frozenset({"404", "NoSuchKey", "NotFound"})


def _is_not_found(exc: ClientError) -> bool:
    """Return True when a ClientError represents a missing object."""
    code = exc.response.get("Error", {}).get("Code")
    return code in _NOT_FOUND_CODES


class S3BlobStore:
    """BlobStore implementation backed by S3 (or any S3-compatible service) via aiobotocore.

    See the module docstring for the connection model, key layout, and the credential /
    endpoint configuration contract.
    """

    def __init__(self, uri: str) -> None:
        """Parse the ``s3://bucket[/prefix]`` URI; open no connection.

        Accepts ``s3://bucket``, ``s3://bucket/prefix``, or
        ``s3://bucket/sub/prefix``. The prefix may be empty; a missing bucket raises
        ``ValueError``. The resolver passes the full URI here — the URI-resolution unit
        test asserts no connection is opened at construction time.
        """
        parts = urlsplit(uri)
        if not parts.netloc:
            raise ValueError(f"s3 URI must include a bucket: {uri!r}")
        self._bucket = parts.netloc
        # ``urlsplit`` leaves a leading slash on the path; strip it so the prefix joins
        # cleanly with the sharded key layout and an empty path yields an empty prefix.
        self._prefix = parts.path.lstrip("/")
        self._session = get_session()
        self._client: Any | None = None
        # AsyncExitStack owns the lifetime of the aiobotocore client async context
        # manager so close() can release it deterministically without re-implementing
        # the __aenter__ / __aexit__ pairing by hand.
        self._exit_stack: AsyncExitStack | None = None
        # Serializes the first-open path so concurrent first-use callers never both enter
        # the aiobotocore client context manager and leak one of the resulting aiohttp
        # connection pools. Lock binds to the running loop on first acquisition, so it is
        # safe to instantiate here.
        self._open_lock = asyncio.Lock()

    # --- Key layout ---

    def _key(self, content_hash: str) -> str:
        """Return the S3 object key for the given content hash.

        Mirrors :meth:`vfs.stores.local_blob.LocalFSBlobStore._path` as a key prefix:
        ``{prefix}/{hash[0:2]}/{hash[2:4]}/{hash}`` (without a prefix segment when the
        configured prefix is empty).
        """
        sharded = f"{content_hash[0:2]}/{content_hash[2:4]}/{content_hash}"
        return f"{self._prefix}/{sharded}" if self._prefix else sharded

    # --- Lifecycle ---

    async def _ensure_client(self) -> Any:
        """Open the aiobotocore client on first use; return the cached client otherwise.

        The client is created via ``session.create_client("s3")`` (an async context
        manager) and entered on an :class:`AsyncExitStack` so :meth:`close` releases it
        cleanly even if it was never opened. Concurrent first-use is serialized by
        ``self._open_lock`` with a double-checked ``self._client`` guard, so at most one
        client (and its underlying ``aiohttp`` pool) is ever created per store instance.
        """
        if self._client is not None:
            return self._client
        async with self._open_lock:
            if self._client is None:
                self._exit_stack = AsyncExitStack()
                client_cm = self._session.create_client("s3")
                self._client = await self._exit_stack.enter_async_context(client_cm)
        return self._client

    async def close(self) -> None:
        """Release the aiobotocore client; safe to call when never opened."""
        if self._exit_stack is not None:
            await self._exit_stack.aclose()
            self._exit_stack = None
        self._client = None

    # --- BlobStore protocol ---

    async def put(self, content_hash: str, data: bytes) -> None:
        """Upload ``data`` under the content-hash key; no-op if it already exists.

        A ``head_object`` short-circuits when the key is already present, matching
        :meth:`LocalFSBlobStore.put`'s idempotence contract. Non-404 ``ClientError``
        propagates.
        """
        client = await self._ensure_client()
        key = self._key(content_hash)
        try:
            await client.head_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            if not _is_not_found(exc):
                raise
        else:
            return  # Already present; idempotent put is a no-op.
        await client.put_object(Bucket=self._bucket, Key=key, Body=data)

    async def get(self, content_hash: str) -> bytes:
        """Return the blob bytes; raise :class:`NotFoundError` when the key is absent."""
        client = await self._ensure_client()
        key = self._key(content_hash)
        try:
            response = await client.get_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            if _is_not_found(exc):
                raise NotFoundError(f"blob {content_hash!r} not found") from exc
            raise
        async with response["Body"] as body:
            return await body.read()

    async def delete(self, content_hash: str) -> None:
        """Delete the object; no-op when the key is absent.

        S3 ``delete_object`` is idempotent and does not raise for missing keys, but a
        ``NoSuchKey`` / 404 surfacing from some S3-compatible backends is also treated
        as a no-op to match :meth:`LocalFSBlobStore.delete`.
        """
        client = await self._ensure_client()
        key = self._key(content_hash)
        try:
            await client.delete_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            if not _is_not_found(exc):
                raise

    async def exists(self, content_hash: str) -> bool:
        """Return True when ``head_object`` succeeds; False on 404/NoSuchKey."""
        client = await self._ensure_client()
        key = self._key(content_hash)
        try:
            await client.head_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            if _is_not_found(exc):
                return False
            raise
        return True

    async def put_stream(self, content_hash: str, stream: AsyncIterator[bytes]) -> None:
        """Not implemented for the S3 store."""
        raise NotImplementedError

    async def get_stream(self, content_hash: str) -> AsyncIterator[bytes]:
        """Not implemented for the S3 store."""
        raise NotImplementedError
        yield b""  # make this an async generator  # pragma: no cover

    async def list_hashes(self) -> AsyncIterator[str]:
        """Enumerate stored content hashes via the ``list_objects_v2`` paginator.

        Each returned key has the form ``{prefix}/{ab}/{cd}/{hash}``; the final
        ``/``-separated segment is the content hash. Keys that don't match the sharded
        layout (e.g. stray top-level objects under the prefix) are skipped defensively.
        """
        client = await self._ensure_client()
        paginator = client.get_paginator("list_objects_v2")
        kwargs: dict[str, Any] = {"Bucket": self._bucket}
        if self._prefix:
            kwargs["Prefix"] = self._prefix
        async for page in paginator.paginate(**kwargs):
            for obj in page.get("Contents") or []:
                key = obj.get("Key")
                if not key:
                    continue
                segments = key.split("/")
                # A well-formed key has at least three trailing segments: ab/cd/hash.
                if len(segments) < 3:
                    continue
                content_hash = segments[-1]
                ab, cd = segments[-3], segments[-2]
                if len(ab) != 2 or len(cd) != 2:
                    continue
                if not content_hash.startswith(ab + cd):
                    continue
                yield content_hash
