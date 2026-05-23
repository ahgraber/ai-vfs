"""Tests for GarbageCollector (Task 20)."""

from __future__ import annotations

import pytest
import pytest_asyncio

from vfs.config import VFSConfig
from vfs.gc import GarbageCollector
from vfs.stores.local_blob import LocalFSBlobStore
from vfs.stores.sqlite_metadata import SQLiteMetadataStore


@pytest_asyncio.fixture
async def make_stores(tmp_path):
    """Factory yielding (meta, blob) stores, closing each metadata store on teardown."""
    created: list[SQLiteMetadataStore] = []

    async def _make():
        meta = SQLiteMetadataStore(":memory:")
        await meta.initialize()
        blob = LocalFSBlobStore(tmp_path / "blobs")
        created.append(meta)
        return meta, blob

    yield _make
    for meta in created:
        await meta.close()


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
    async def test_version_gc_respects_max_recent(self, make_stores):
        meta, blob = await make_stores()
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
    async def test_version_gc_keeps_first_version(self, make_stores):
        meta, blob = await make_stores()
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
    async def test_version_gc_keeps_current(self, make_stores):
        meta, blob = await make_stores()
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
    async def test_blob_gc_removes_orphaned_blobs(self, make_stores):
        meta, blob = await make_stores()
        config = VFSConfig(retention_max_recent=50, audit_log_enabled=False)
        # Put a blob manually (no version references it)
        await blob.put("orphaned_hash_0000000000000000", b"orphan data")
        gc = GarbageCollector(meta, blob, config)
        result = await gc.run()
        assert result.blobs_reclaimed == 1
        assert not await blob.exists("orphaned_hash_0000000000000000")

    @pytest.mark.asyncio
    async def test_blob_gc_keeps_referenced_blobs(self, make_stores):
        meta, blob = await make_stores()
        config = VFSConfig(retention_max_recent=50, audit_log_enabled=False)
        await blob.put("referenced_hash_00000000000000", b"data")
        v = _version("ns1", "/a.py", 1, "referenced_hash_00000000000000")
        await meta.put_version(v, expected_version=None)
        gc = GarbageCollector(meta, blob, config)
        result = await gc.run()
        assert result.blobs_reclaimed == 0
        assert await blob.exists("referenced_hash_00000000000000")

    @pytest.mark.asyncio
    async def test_gc_creates_audit_event(self, make_stores):
        meta, blob = await make_stores()
        config = VFSConfig(retention_max_recent=50, audit_log_enabled=True)
        gc = GarbageCollector(meta, blob, config)
        await gc.run()
        rows = await meta._execute_fetchall("SELECT operation FROM audit_events")
        ops = [r[0] for r in rows]
        assert "gc_run" in ops

    @pytest.mark.asyncio
    async def test_gc_run_sets_process_title(self, make_stores, monkeypatch):
        """ProcessIdentification (design D11): GarbageCollector.run sets the process title."""
        import setproctitle

        captured: list[str] = []
        monkeypatch.setattr(setproctitle, "setproctitle", lambda t: captured.append(t))
        meta, blob = await make_stores()
        config = VFSConfig(retention_max_recent=50, audit_log_enabled=False)
        gc = GarbageCollector(meta, blob, config)
        await gc.run()
        assert "ai-vfs: gc" in captured

    @pytest.mark.asyncio
    async def test_audit_log_survives_gc(self, make_stores):
        """AuditLogAppendOnly: GC reclaims versions/blobs but MUST NOT touch audit_events."""
        from datetime import datetime, timezone

        from ulid import ULID

        from vfs.models import AuditEvent

        meta, blob = await make_stores()
        config = VFSConfig(retention_max_recent=1, audit_log_enabled=False)
        # Seed an audit event for an unrelated prior operation.
        seeded = AuditEvent(
            event_id=str(ULID()),
            timestamp=datetime.now(timezone.utc),
            namespace_id="ns1",
            principal_id="p1",
            operation="write",
            path="/a.py",
        )
        await meta.append_audit_event(seeded)
        # Populate enough versions to trigger reclamation.
        for i in range(1, 5):
            v = _version("ns1", "/a.py", i, f"h{i}")
            ev = None if i == 1 else i - 1
            await meta.put_version(v, expected_version=ev)
        # Drop an orphaned blob.
        await blob.put("orphan_hash_xxxxxxxxxxxxxxxxxxxxx", b"orphan")

        before = await meta._execute_fetchall("SELECT event_id FROM audit_events")
        before_ids = sorted(r[0] for r in before)
        assert seeded.event_id in before_ids

        gc = GarbageCollector(meta, blob, config)
        await gc.run("ns1")

        after = await meta._execute_fetchall("SELECT event_id FROM audit_events")
        after_ids = sorted(r[0] for r in after)
        # Seeded audit row MUST survive; GC adds no audit rows when audit_log_enabled=False.
        assert seeded.event_id in after_ids
        assert after_ids == before_ids

    @pytest.mark.asyncio
    async def test_gc_cross_namespace_blob_preservation(self, make_stores):
        """VersionGarbageCollection / GCPreservesSharedBlobs:
        a content_hash referenced by a version in ns2 SHALL NOT be deleted when GC runs on ns1.
        """
        meta, blob = await make_stores()
        config = VFSConfig(retention_max_recent=1, audit_log_enabled=False)
        shared_hash = "shared_hash_xxxxxxxxxxxxxxxxxxxxxxxx"
        await blob.put(shared_hash, b"shared content")
        # ns1: 3 versions of /a.py with the shared content; older ones become reclaimable.
        for i in range(1, 4):
            v = _version("ns1", "/a.py", i, shared_hash)
            ev = None if i == 1 else i - 1
            await meta.put_version(v, expected_version=ev)
        # ns2: a single version pointing at the same hash — this reference must keep the blob alive.
        v_other = _version("ns2", "/b.py", 1, shared_hash)
        await meta.put_version(v_other, expected_version=None)

        gc = GarbageCollector(meta, blob, config)
        result = await gc.run("ns1")
        # Some ns1 versions reclaimed, but blob retained because ns2 still references it.
        assert result.versions_reclaimed >= 1
        assert await blob.exists(shared_hash), "shared blob deleted despite cross-namespace ref"
