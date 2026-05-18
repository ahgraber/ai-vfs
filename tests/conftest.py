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


@pytest_asyncio.fixture
async def admin_factory(vfs_instance):
    """Return an async callable that bootstraps a fresh admin principal in a namespace."""

    async def make(ns_id: str, display_name: str = "test-admin"):
        admin = await vfs_instance.create_principal(display_name)
        await vfs_instance.bootstrap_admin(admin.id, ns_id)
        return admin

    return make


@pytest_asyncio.fixture
async def otel_vfs_instance(tmp_path):
    """VFS instance with otel_enabled=True for span/metric instrumentation tests."""
    db_path = str(tmp_path / "test_otel.db")
    blob_path = str(tmp_path / "blobs_otel")
    config = VFSConfig(
        metadata_store_uri=f"sqlite:///{db_path}",
        blob_store_uri=f"file:///{blob_path}/",
        otel_enabled=True,
        audit_log_enabled=True,
    )
    vfs = VFS(config)
    await vfs.initialize()
    yield vfs
    await vfs.close()
