"""Shared test fixtures."""

from __future__ import annotations

import pytest_asyncio

from vfs.config import VFSConfig
from vfs.vfs import VFS


@pytest_asyncio.fixture
async def vfs_instance(tmp_path):
    db_path = str(tmp_path / "test.db")
    blob_path = str(tmp_path / "blobs")
    config = VFSConfig(
        metadata_store_uri=f"sqlite:///{db_path}",
        blob_store_uri=f"file:///{blob_path}/",
        otel_enabled=False,
        audit_log_enabled=True,
    )
    vfs = VFS(config)
    await vfs.initialize()
    yield vfs
    await vfs.close()
