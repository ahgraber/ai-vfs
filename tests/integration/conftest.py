"""Shared setup for ai-vfs integration tests.

Defaults the service-endpoint env vars to the local Docker/Podman compose stack
(``tests/integration/docker-compose.yaml``) when they are unset, so
``uv run pytest tests/integration`` runs against it after ``podman compose up`` without
per-shell env wrangling. This lives here, not in the Nix flake, because it is test-harness
configuration: it must apply wherever pytest runs (CI, a bare venv, the devshell), and the
compose file that defines these values already lives in this directory.

``setdefault`` means explicit exports still win, and each suite skips when its service is
unreachable — so CI without these services is unaffected.

**S3/MinIO bucket** is provisioned *ephemerally* here (``pytest_sessionstart`` /
``pytest_sessionfinish``): a per-worker bucket is created clean at session start, pointed at via
``AIVFS_TEST_S3_BUCKET`` before the test modules import, and deleted at session end. Combined
with MinIO's volume-less ``/data`` in the compose file, nothing persists to disk or carries
across runs. Per-worker bucket names avoid xdist collisions. If ``aiobotocore`` is missing or
MinIO is unreachable, provisioning is skipped silently and the S3 tests skip as before. An
explicit ``AIVFS_TEST_S3_BUCKET`` is respected and left unmanaged.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os

# --- service-endpoint defaults → the local compose stack ---
os.environ.setdefault("AIVFS_TEST_POSTGRES_DSN", "postgresql://aivfs:aivfs@localhost:5432/aivfs")
os.environ.setdefault("AIVFS_TEST_MONGO_URI", "mongodb://localhost:27017/aivfs")
# S3/MinIO connection (the bucket itself is provisioned ephemerally below).
os.environ.setdefault("AWS_ACCESS_KEY_ID", "minioadmin")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "minioadmin")
os.environ.setdefault("AWS_ENDPOINT_URL_S3", "http://localhost:9000")
os.environ.setdefault("AWS_REGION", "us-east-1")


def _ephemeral_bucket_name() -> str:
    """Per-worker bucket name (S3-valid: lowercase, no underscores)."""
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    return f"aivfs-test-{worker}".lower()


async def _empty_bucket(client, bucket: str) -> None:
    paginator = client.get_paginator("list_objects_v2")
    async for page in paginator.paginate(Bucket=bucket):
        contents = page.get("Contents") or []
        if contents:
            await client.delete_objects(
                Bucket=bucket,
                Delete={"Objects": [{"Key": obj["Key"]} for obj in contents]},
            )


async def _s3_bucket_op(op: str, bucket: str) -> bool:
    """Create-clean (``op="create"``) or delete (``op="delete"``) an ephemeral MinIO bucket.

    Returns True on success. Best-effort: a missing ``aiobotocore`` or any connection/client
    error returns False so the S3 tests skip rather than failing the run.
    """
    if importlib.util.find_spec("aiobotocore") is None:
        return False
    from aiobotocore.session import get_session
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, ClientError

    session = get_session()
    # MinIO requires path-style addressing (no bucket-as-subdomain).
    config = Config(s3={"addressing_style": "path"})
    try:
        async with session.create_client(
            "s3", endpoint_url=os.environ.get("AWS_ENDPOINT_URL_S3"), config=config
        ) as client:
            if op == "create":
                try:
                    await client.create_bucket(Bucket=bucket)
                except ClientError:
                    pass  # already exists (e.g. a crashed prior run) — fall through to clean it
                await _empty_bucket(client, bucket)  # ensure a clean bucket
            elif op == "delete":
                await _empty_bucket(client, bucket)
                try:
                    await client.delete_bucket(Bucket=bucket)
                except ClientError:
                    pass
    except (BotoCoreError, ClientError, OSError):
        return False
    else:
        return True


def pytest_sessionstart(session) -> None:  # noqa: ARG001 — pytest hook signature
    """Provision an ephemeral per-worker MinIO bucket and point the S3 tests at it.

    Runs before collection, so ``test_s3_blob`` reads the bucket name from
    ``AIVFS_TEST_S3_BUCKET`` at import. An explicit value is respected and left unmanaged.
    """
    if os.environ.get("AIVFS_TEST_S3_BUCKET"):
        return  # caller supplied a bucket — respect it, don't manage its lifecycle
    bucket = _ephemeral_bucket_name()
    if asyncio.run(_s3_bucket_op("create", bucket)):
        os.environ["AIVFS_TEST_S3_BUCKET"] = bucket


def pytest_sessionfinish(session, exitstatus) -> None:  # noqa: ARG001 — pytest hook signature
    """Tear down the ephemeral bucket provisioned in ``pytest_sessionstart``."""
    bucket = os.environ.get("AIVFS_TEST_S3_BUCKET")
    if bucket and bucket == _ephemeral_bucket_name():
        asyncio.run(_s3_bucket_op("delete", bucket))
