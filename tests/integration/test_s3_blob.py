"""Integration tests for S3BlobStore against a real S3-compatible service.

These tests require a reachable S3 endpoint and a pre-created bucket. Provide the bucket
name via the ``AIVFS_TEST_S3_BUCKET`` environment variable and the standard botocore
credentials/region/endpoint environment variables (e.g. for a local MinIO)::

    export AIVFS_TEST_S3_BUCKET=aivfs-test
    export AWS_ACCESS_KEY_ID=minioadmin
    export AWS_SECRET_ACCESS_KEY=minioadmin
    export AWS_ENDPOINT_URL_S3=http://localhost:9000
    export AWS_REGION=us-east-1

Start a local MinIO server with the Docker Compose fixture::

    docker compose -f tests/integration/docker-compose.yaml up -d minio

The whole module is skipped when ``aiobotocore`` is not installed, the bucket env var is
unset, or the endpoint is unreachable, so the default test run stays green without S3.
The bucket itself must be pre-created (the tests isolate by per-worker key prefix, not by
bucket name).
"""

from __future__ import annotations

import importlib.util
import os

import pytest
import pytest_asyncio
from ulid import ULID

_BUCKET = os.environ.get("AIVFS_TEST_S3_BUCKET")

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("aiobotocore") is None or not _BUCKET,
    reason="requires aiobotocore and AIVFS_TEST_S3_BUCKET pointing at a reachable S3 bucket",
)


@pytest_asyncio.fixture
async def s3_store():
    """An S3BlobStore scoped to a unique per-worker key prefix in the shared bucket.

    Each xdist worker uses its own prefix ``aivfs-test/<worker>/<ulid>`` so parallel
    workers never collide on object keys. Teardown deletes every object under the prefix
    on a best-effort basis and releases the aiobotocore client; both reachability
    failures during setup and ClientErrors during cleanup are tolerated so a flaky
    endpoint produces a skip, not a hard failure.
    """
    from botocore.exceptions import ClientError, EndpointConnectionError

    from vfs.stores.s3_blob import S3BlobStore

    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    prefix = f"aivfs-test/{worker}/{ULID()}"
    store = S3BlobStore(f"s3://{_BUCKET}/{prefix}")

    # Probe the endpoint up front so an unreachable service produces a clean skip rather
    # than failing every test in the module on the first put/get/etc.
    try:
        client = await store._ensure_client()
        await client.head_bucket(Bucket=_BUCKET)
    except (ClientError, EndpointConnectionError, OSError) as exc:
        await store.close()
        pytest.skip(f"S3 bucket {_BUCKET!r} unreachable: {exc}")

    try:
        yield store
    finally:
        # Best-effort cleanup: enumerate every object under the prefix and delete it.
        try:
            client = await store._ensure_client()
            paginator = client.get_paginator("list_objects_v2")
            async for page in paginator.paginate(Bucket=_BUCKET, Prefix=prefix):
                contents = page.get("Contents") or []
                if not contents:
                    continue
                # delete_objects accepts up to 1000 keys per call; the test fixtures
                # never approach that bound, so a single call per page is sufficient.
                await client.delete_objects(
                    Bucket=_BUCKET,
                    Delete={"Objects": [{"Key": obj["Key"]} for obj in contents]},
                )
        except (ClientError, EndpointConnectionError, OSError):
            pass  # best-effort teardown
        finally:
            await store.close()


@pytest.mark.asyncio
async def test_round_trip_and_idempotent_put(s3_store):
    """S3AdapterRoundTrip: put(hash, data) then get(hash) returns data; a second put of
    the same hash with different data is a no-op (head_object short-circuits) and the
    original bytes are preserved."""
    content_hash = "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
    data = b"original content"
    await s3_store.put(content_hash, data)
    assert await s3_store.get(content_hash) == data

    # Idempotent: a second put with the SAME hash must not overwrite the stored bytes.
    await s3_store.put(content_hash, b"different content")
    assert await s3_store.get(content_hash) == data


@pytest.mark.asyncio
async def test_key_structure(s3_store):
    """S3KeyStructure: a put produces exactly one object at
    ``{prefix}/{hash[0:2]}/{hash[2:4]}/{hash}`` — observed via a raw aiobotocore list."""
    content_hash = "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
    await s3_store.put(content_hash, b"payload")

    expected_key = f"{s3_store._prefix}/ab/cd/{content_hash}"
    client = await s3_store._ensure_client()
    response = await client.list_objects_v2(Bucket=_BUCKET, Prefix=s3_store._prefix)
    keys = [obj["Key"] for obj in response.get("Contents") or []]
    assert keys == [expected_key]


@pytest.mark.asyncio
async def test_exists_and_delete(s3_store):
    """exists is False before put and True after; delete returns the store to False; a
    second delete is a no-op (S3 delete_object is idempotent)."""
    content_hash = "aa" * 32
    assert await s3_store.exists(content_hash) is False
    await s3_store.put(content_hash, b"data")
    assert await s3_store.exists(content_hash) is True

    await s3_store.delete(content_hash)
    assert await s3_store.exists(content_hash) is False
    # Second delete must not raise.
    await s3_store.delete(content_hash)


@pytest.mark.asyncio
async def test_list_hashes(s3_store):
    """list_hashes yields exactly the inserted hashes (defensive filtering ignores keys
    that don't match the sharded layout, but we never write any here)."""
    hashes = {
        "aa" * 32,
        "bb" * 32,
        "cc" * 32,
    }
    for h in hashes:
        await s3_store.put(h, h.encode())

    found = set()
    async for h in s3_store.list_hashes():
        found.add(h)
    assert found == hashes


@pytest.mark.asyncio
async def test_list_hashes_skips_decoy_non_hash_keys(s3_store):
    """list_hashes' defensive filter must skip keys under the prefix that do NOT match the
    sharded ``{ab}/{cd}/{hash}`` layout — both stray top-level objects and path-shaped-
    but-invalid keys where the trailing hash does not begin with ``{ab}{cd}``."""
    good_hash = "aabbccdd" + "0" * 56
    await s3_store.put(good_hash, b"data")

    # Inject decoys under the same prefix via the raw aiobotocore client.
    client = await s3_store._ensure_client()
    decoy_top_level = f"{s3_store._prefix}/junk.txt"
    decoy_pathlike = f"{s3_store._prefix}/zz/zz/garbage"
    await client.put_object(Bucket=_BUCKET, Key=decoy_top_level, Body=b"x")
    await client.put_object(Bucket=_BUCKET, Key=decoy_pathlike, Body=b"x")

    found = set()
    async for h in s3_store.list_hashes():
        found.add(h)
    assert found == {good_hash}


@pytest.mark.asyncio
async def test_get_missing_raises_not_found(s3_store):
    """get on an unknown hash raises NotFoundError, not the raw ClientError."""
    from vfs.errors import NotFoundError

    with pytest.raises(NotFoundError):
        await s3_store.get("deadbeef" * 8)
