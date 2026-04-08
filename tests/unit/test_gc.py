"""Tests for GarbageCollector (Task 20)."""

from __future__ import annotations

import pytest
import pytest_asyncio

from vfs.config import VFSConfig
from vfs.gc import GarbageCollector
from vfs.stores.local_blob import LocalFSBlobStore
from vfs.stores.sqlite_metadata import SQLiteMetadataStore


async def _make_stores(tmp_path):
    meta = SQLiteMetadataStore(":memory:")
    await meta.initialize()
    blob = LocalFSBlobStore(tmp_path / "blobs")
    return meta, blob


def _version(ns, path, num, content_hash="h1"):
    from datetime import datetime, timezone

    from ulid import ULID

    from vfs.models import VersionMeta

    return VersionMeta(
        id=str(ULID()),
        file_path=path,
        namespace_id=ns,
        version_number=num,
        content_hash=content_hash,
        size=4,
        created_at=datetime.now(timezone.utc),
        created_by="p1",
    )


class TestGarbageCollector:
    @pytest.mark.asyncio
    async def test_version_gc_respects_max_recent(self, tmp_path):
        meta, blob = await _make_stores(tmp_path)
        config = VFSConfig(retention_max_recent=2, audit_log_enabled=False)
        for i in range(1, 6):
            v = _version("ns1", "/a.py", i, f"h{i}")
            ev = None if i == 1 else i - 1
            await meta.put_version(v, expected_version=ev)
        gc = GarbageCollector(meta, blob, config)
        result = await gc.run("ns1")
        # Keep v1 (first), v4, v5 (2 most recent) → reclaim v2, v3
        assert result.versions_reclaimed == 2

    @pytest.mark.asyncio
    async def test_version_gc_keeps_first_version(self, tmp_path):
        meta, blob = await _make_stores(tmp_path)
        config = VFSConfig(retention_max_recent=1, audit_log_enabled=False)
        for i in range(1, 4):
            v = _version("ns1", "/a.py", i, f"h{i}")
            ev = None if i == 1 else i - 1
            await meta.put_version(v, expected_version=ev)
        gc = GarbageCollector(meta, blob, config)
        await gc.run("ns1")
        versions = await meta.list_versions("ns1", "/a.py")
        nums = {v.version_number for v in versions}
        assert 1 in nums  # first version kept

    @pytest.mark.asyncio
    async def test_version_gc_keeps_current(self, tmp_path):
        meta, blob = await _make_stores(tmp_path)
        config = VFSConfig(retention_max_recent=1, audit_log_enabled=False)
        for i in range(1, 4):
            v = _version("ns1", "/a.py", i, f"h{i}")
            ev = None if i == 1 else i - 1
            await meta.put_version(v, expected_version=ev)
        gc = GarbageCollector(meta, blob, config)
        await gc.run("ns1")
        versions = await meta.list_versions("ns1", "/a.py")
        nums = {v.version_number for v in versions}
        assert 3 in nums  # current version kept

    @pytest.mark.asyncio
    async def test_blob_gc_removes_orphaned_blobs(self, tmp_path):
        meta, blob = await _make_stores(tmp_path)
        config = VFSConfig(retention_max_recent=50, audit_log_enabled=False)
        # Put a blob manually (no version references it)
        await blob.put("orphaned_hash_0000000000000000", b"orphan data")
        gc = GarbageCollector(meta, blob, config)
        result = await gc.run()
        assert result.blobs_reclaimed == 1
        assert not await blob.exists("orphaned_hash_0000000000000000")

    @pytest.mark.asyncio
    async def test_blob_gc_keeps_referenced_blobs(self, tmp_path):
        meta, blob = await _make_stores(tmp_path)
        config = VFSConfig(retention_max_recent=50, audit_log_enabled=False)
        await blob.put("referenced_hash_00000000000000", b"data")
        v = _version("ns1", "/a.py", 1, "referenced_hash_00000000000000")
        await meta.put_version(v, expected_version=None)
        gc = GarbageCollector(meta, blob, config)
        result = await gc.run()
        assert result.blobs_reclaimed == 0
        assert await blob.exists("referenced_hash_00000000000000")

    @pytest.mark.asyncio
    async def test_gc_creates_audit_event(self, tmp_path):
        meta, blob = await _make_stores(tmp_path)
        config = VFSConfig(retention_max_recent=50, audit_log_enabled=True)
        gc = GarbageCollector(meta, blob, config)
        await gc.run()
        rows = await meta._execute_fetchall("SELECT operation FROM audit_events")
        ops = [r[0] for r in rows]
        assert "gc_run" in ops
