"""Tests for GarbageCollector (Task 20)."""

from __future__ import annotations

from datetime import datetime

import pytest
import pytest_asyncio

from vfs.config import VFSConfig
from vfs.gc import GarbageCollector
from vfs.models import VersionMeta
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
    async def test_gc_run_uses_tier_path_when_retention_tiers_configured(self, make_stores):
        """TierGCWiredToRun: GarbageCollector.run() routes to _tier_version_gc when
        config.retention_tiers is non-empty, and tier reclamation produces the expected count.

        Policy: single tier, max_age=10 years, keep_every=10 years.  All versions created
        "just now" land in window 0 (age≈0 / huge_window = 0).  Window survivor = v1
        (min created_at / version_number tie-breaker).  keep_first_version keeps v1,
        keep_current_version keeps v4.  v2 and v3 are reclaimable → 2 reclaimed.
        """
        # 10 years in seconds — all freshly-created versions share window 0.
        _10yr_secs = 86400 * 365 * 10
        meta, blob = await make_stores()
        config = VFSConfig(
            retention_max_recent=50,
            retention_tiers=[{"max_age": _10yr_secs, "keep_every": _10yr_secs}],
            audit_log_enabled=False,
        )
        for i in range(1, 5):
            v = _version("ns1", "/a.py", i, f"h{i}")
            ev = None if i == 1 else i - 1
            await meta.put_version(v, expected_version=ev)
        gc = GarbageCollector(meta, blob, config)
        result = await gc.run("ns1")
        # v1 kept (first + window survivor), v4 kept (current), v2+v3 reclaimed.
        assert result.versions_reclaimed == 2

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


# ---------------------------------------------------------------------------
# TierBasedRetention — pure evaluate_tier_retention unit tests
# ---------------------------------------------------------------------------


def _tier_version(
    ns: str,
    path: str,
    num: int,
    created_at: datetime,
    content_hash: str = "h",
) -> VersionMeta:
    """Build a VersionMeta with a specific created_at for tier tests."""
    from ulid import ULID

    return VersionMeta(
        id=str(ULID()),
        file_path=path,
        namespace_id=ns,
        version_number=num,
        content_hash=f"{content_hash}{num}",
        size=4,
        created_at=created_at,
        created_by="p1",
    )


class TestTierEvaluator:
    """Unit tests for evaluate_tier_retention — no store required."""

    def test_hourly_tier_keeps_one_per_hour(self):
        """HourlyTierKeepsOnePerHour: 60 versions over 6 hours with (24h, 1h) tier.

        Expected retained: 6 hourly window survivors (the oldest per window) + the
        current version (newest in its window, not a survivor) = 7 total.
        """
        from datetime import timedelta, timezone

        from vfs.gc import evaluate_tier_retention
        from vfs.models import RetentionPolicy, RetentionTier

        now = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        # 10 versions per hourly window, with version_number 1 being oldest.
        # Constructed so all 10 versions in each window have ages strictly within
        # [window*3600, (window+1)*3600) seconds — avoiding the boundary edge case
        # where age == N*3600 would push a version into the next window.
        #
        # Window 5 (age 5h–5h59m): versions 1–10  (v1 = oldest overall = keep_first)
        # Window 4 (age 4h–4h59m): versions 11–20
        # ...
        # Window 0 (age 0–59m):    versions 51–60  (v60 = newest = keep_current)
        versions = []
        version_num = 1
        for window_idx in range(5, -1, -1):  # oldest windows first so v1 is in window 5
            for k in range(9, -1, -1):  # oldest-within-window first so v1 is the window-5 survivor
                # Age is strictly inside the window: window*3600 + k*300 + 60 seconds.
                age_secs = window_idx * 3600 + k * 300 + 60
                created = now - timedelta(seconds=age_secs)
                versions.append(_tier_version("ns", "/f", version_num, created))
                version_num += 1

        policy = RetentionPolicy(
            max_recent_versions=0,
            tiers=[RetentionTier(max_age=timedelta(hours=24), keep_every=timedelta(hours=1))],
            keep_first_version=True,
            keep_current_version=True,
        )
        current_version_id = max(versions, key=lambda v: v.version_number).id

        reclaimable = evaluate_tier_retention(versions, policy, now, current_version_id)
        retained = {v.id for v in versions} - reclaimable

        assert len(retained) == 7, f"expected 7 retained, got {len(retained)}"
        # The current version must be in the retained set.
        assert current_version_id in retained
        # The first version (v1, oldest) must be in the retained set.
        first_id = min(versions, key=lambda v: (v.created_at, v.version_number)).id
        assert first_id in retained

    def test_tiers_cascade_newest_first(self):
        """TiersCascadeNewestFirst: 4-band policy with versions spanning 60 days.

        Verifies that versions are partitioned into the correct age bands:
        <24h → all kept; 24h–7d → sampled hourly; 7d–30d → sampled daily; 30d–365d → sampled weekly.
        """
        from datetime import timedelta, timezone

        from vfs.gc import evaluate_tier_retention
        from vfs.models import RetentionPolicy, RetentionTier

        now = datetime(2024, 3, 1, 0, 0, 0, tzinfo=timezone.utc)
        policy = RetentionPolicy(
            max_recent_versions=0,
            tiers=[
                RetentionTier(max_age=timedelta(hours=24), keep_every=None),
                RetentionTier(max_age=timedelta(days=7), keep_every=timedelta(hours=1)),
                RetentionTier(max_age=timedelta(days=30), keep_every=timedelta(days=1)),
                RetentionTier(max_age=timedelta(days=365), keep_every=timedelta(weeks=1)),
            ],
            keep_first_version=True,
            keep_current_version=True,
        )

        # One version per 6 hours over 60 days = 240 versions.
        versions = []
        n = 240
        for i in range(n):
            age = timedelta(hours=6 * i)
            created = now - age
            versions.append(_tier_version("ns", "/f", n - i, created))  # newest = v240

        current_version_id = max(versions, key=lambda v: v.version_number).id
        reclaimable = evaluate_tier_retention(versions, policy, now, current_version_id)
        retained = {v.id for v in versions} - reclaimable

        # Band <24h (age 0–24h): 4 versions (i=0..3), all kept.
        band_lt24h = [v for v in versions if (now - v.created_at) < timedelta(hours=24)]
        assert all(v.id in retained for v in band_lt24h), "all <24h versions must be retained"

        # Band 24h–7d: sampled hourly → at most one per hourly window.
        band_24h_7d = [v for v in versions if timedelta(hours=24) <= (now - v.created_at) < timedelta(days=7)]
        # With 6h-spaced versions in a 1h window there is at most 1 version per window anyway,
        # so ALL of them are window survivors and must be retained.
        assert all(v.id in retained for v in band_24h_7d), (
            "all 24h–7d versions must be retained (each in its own hourly window)"
        )

        # Band 7d–30d: sampled daily → 1 per day.
        band_7d_30d = [v for v in versions if timedelta(days=7) <= (now - v.created_at) < timedelta(days=30)]
        kept_7d_30d = [v for v in band_7d_30d if v.id in retained]
        # With 6h spacing, at most 4 versions per day; we keep 1 per day → kept_7d_30d ≤ band_7d_30d.
        assert len(kept_7d_30d) <= len(band_7d_30d)
        # At most 1 retained per daily window.
        from collections import Counter

        day_counts = Counter(int((now - v.created_at).total_seconds() / 86400) for v in kept_7d_30d)
        assert max(day_counts.values()) == 1, "at most one version per daily window"

        # Band 30d–365d (60 days): sampled weekly; versions from day 30 to day 60.
        band_30d_365d = [v for v in versions if timedelta(days=30) <= (now - v.created_at) < timedelta(days=365)]
        kept_30d_365d = [v for v in band_30d_365d if v.id in retained]
        assert len(kept_30d_365d) <= len(band_30d_365d)
        week_secs = timedelta(weeks=1).total_seconds()
        week_counts = Counter(int((now - v.created_at).total_seconds() / week_secs) for v in kept_30d_365d)
        assert all(c == 1 for c in week_counts.values()), "at most one version per weekly window"

        # Versions beyond 365d: none in this dataset; just confirm no KeyError.
        beyond = [v for v in versions if (now - v.created_at) >= timedelta(days=365)]
        assert len(beyond) == 0

    def test_first_within_window_is_deterministic(self):
        """FirstWithinWindowIsDeterministic: survivor is min(created_at) regardless of input order."""
        from datetime import timedelta, timezone

        from vfs.gc import evaluate_tier_retention
        from vfs.models import RetentionPolicy, RetentionTier

        now = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        # Three versions in the same hourly window (ages 2h10m, 2h20m, 2h30m).
        v12 = _tier_version("ns", "/f", 12, now - timedelta(hours=2, minutes=30))
        v13 = _tier_version("ns", "/f", 13, now - timedelta(hours=2, minutes=20))
        v14 = _tier_version("ns", "/f", 14, now - timedelta(hours=2, minutes=10))

        policy = RetentionPolicy(
            max_recent_versions=0,
            tiers=[RetentionTier(max_age=timedelta(hours=24), keep_every=timedelta(hours=1))],
            keep_first_version=False,
            keep_current_version=False,
        )

        # In canonical order (oldest first):
        reclaimable_fwd = evaluate_tier_retention([v12, v13, v14], policy, now, None)
        # In reverse order:
        reclaimable_rev = evaluate_tier_retention([v14, v13, v12], policy, now, None)
        # In shuffled order:
        reclaimable_shuf = evaluate_tier_retention([v13, v14, v12], policy, now, None)

        # In every case v12 (oldest in the window, smallest created_at) must survive.
        assert v12.id not in reclaimable_fwd
        assert v12.id not in reclaimable_rev
        assert v12.id not in reclaimable_shuf
        # v13 and v14 are always reclaimable (same window, not the survivor).
        assert v13.id in reclaimable_fwd and v14.id in reclaimable_fwd
        assert reclaimable_fwd == reclaimable_rev == reclaimable_shuf

    @pytest.mark.asyncio
    async def test_iter_versions_for_gc_sqlite(self, tmp_path):
        """SQLite iter_versions_for_gc returns non-tombstone versions ordered by created_at, version_number."""
        from datetime import timedelta, timezone

        from vfs.stores.sqlite_metadata import SQLiteMetadataStore

        store = SQLiteMetadataStore(":memory:")
        await store.initialize()

        now = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        # Insert 3 versions with explicit created_at values (out of sequence to prove ordering).
        v2 = _tier_version("ns", "/f.py", 2, now - timedelta(hours=1))
        v1 = _tier_version("ns", "/f.py", 1, now - timedelta(hours=2))
        v3 = _tier_version("ns", "/f.py", 3, now - timedelta(minutes=30))

        await store.put_version(v1, expected_version=None)
        await store.put_version(v2, expected_version=1)
        await store.put_version(v3, expected_version=2)

        collected = [v async for v in store.iter_versions_for_gc("ns", "/f.py")]
        assert [v.version_number for v in collected] == [1, 2, 3]

        await store.close()

    @pytest.mark.asyncio
    async def test_tier_gc_sqlite_reclamation(self, make_stores):
        """SQLite _tier_version_gc deletes the expected versions for a fixed policy (SQLite leg of ReclamationIdenticalAcrossAdapters)."""
        from datetime import timedelta, timezone

        from vfs.gc import GarbageCollector, evaluate_tier_retention
        from vfs.models import RetentionPolicy, RetentionTier

        meta, blob = await make_stores()
        config = VFSConfig(retention_max_recent=50, audit_log_enabled=False)
        gc = GarbageCollector(meta, blob, config)

        now = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        # 10 versions: 2 per hour over 5 hours.
        versions = []
        for hour in range(4, -1, -1):
            for minute in (0, 30):
                version_num = (4 - hour) * 2 + (1 if minute == 0 else 2)
                created = now - timedelta(hours=hour + 1) + timedelta(minutes=minute)
                versions.append(_tier_version("ns", "/f.py", version_num, created))

        # Insert in version_number order.
        for i, v in enumerate(sorted(versions, key=lambda v: v.version_number)):
            ev = None if i == 0 else i
            await meta.put_version(v, expected_version=ev)

        policy = RetentionPolicy(
            max_recent_versions=0,
            tiers=[RetentionTier(max_age=timedelta(hours=24), keep_every=timedelta(hours=1))],
            keep_first_version=True,
            keep_current_version=True,
        )

        # Pre-compute expected reclaimable set from the pure evaluator.
        all_versions = sorted(versions, key=lambda v: (v.created_at, v.version_number))
        current_version_id = max(versions, key=lambda v: v.version_number).id
        expected_reclaimable = evaluate_tier_retention(all_versions, policy, now, current_version_id)

        reclaimed_count = await gc._tier_version_gc("ns", policy, now=now)

        assert reclaimed_count == len(expected_reclaimable)
        # Verify surviving versions are the expected ones.
        remaining = await meta.list_versions("ns", "/f.py", limit=50)
        remaining_ids = {v.id for v in remaining}
        expected_retained = {v.id for v in versions} - expected_reclaimable
        assert remaining_ids == expected_retained
