"""Tests for SQLiteMetadataStore."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import pytest_asyncio

from vfs.errors import ConflictError
from vfs.models import (
    AuditEvent,
    FileMeta,
    Permission,
    RetentionPolicy,
    VersionMeta,
)
from vfs.protocols.metadata import MetadataStore
from vfs.stores.sqlite_metadata import SQLiteMetadataStore


@pytest_asyncio.fixture
async def sqlite_store():
    store = SQLiteMetadataStore(":memory:")
    await store.initialize()
    yield store
    await store.close()


def _now():
    return datetime.now(timezone.utc)


def _version(ns: str, path: str, num: int, *, content_hash: str = "hash1", is_tombstone: bool = False) -> VersionMeta:
    from ulid import ULID

    return VersionMeta(
        id=str(ULID()),
        file_path=path,
        namespace_id=ns,
        version_number=num,
        content_hash=content_hash,
        size=42,
        created_at=_now(),
        created_by="principal1",
        is_tombstone=is_tombstone,
    )


class TestSQLiteMetadataStoreSchema:
    """Task 6: schema and initialization."""

    @pytest.mark.asyncio
    async def test_initialize_creates_tables(self, sqlite_store):
        rows = await sqlite_store._execute_fetchall("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        table_names = {row[0] for row in rows}
        expected = {
            "namespaces",
            "principals",
            "files",
            "versions",
            "permissions",
            "audit_events",
            "names",
        }
        assert expected.issubset(table_names)


class TestFileAndVersionOps:
    """Task 7: file and version operations with CAS."""

    @pytest.mark.asyncio
    async def test_put_and_get_file(self, sqlite_store):
        now = _now()
        meta = FileMeta(
            namespace_id="ns1",
            path="/src/a.py",
            current_version_id="v1",
            current_version_number=1,
            created_at=now,
            updated_at=now,
        )
        await sqlite_store.put_file(meta)
        result = await sqlite_store.get_file("ns1", "/src/a.py")
        assert result is not None
        assert result.path == "/src/a.py"

    @pytest.mark.asyncio
    async def test_get_file_missing(self, sqlite_store):
        result = await sqlite_store.get_file("ns1", "/nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_list_dir_non_recursive(self, sqlite_store):
        now = _now()
        for p in ["/src/a.py", "/src/b.py", "/src/sub/c.py"]:
            await sqlite_store.put_file(
                FileMeta(
                    namespace_id="ns1",
                    path=p,
                    current_version_id="v1",
                    current_version_number=1,
                    created_at=now,
                    updated_at=now,
                )
            )
        results = await sqlite_store.list_dir("ns1", "/src/", recursive=False)
        paths = {r.path for r in results}
        assert paths == {"/src/a.py", "/src/b.py"}

    @pytest.mark.asyncio
    async def test_list_dir_recursive(self, sqlite_store):
        now = _now()
        for p in ["/src/a.py", "/src/b.py", "/src/sub/c.py"]:
            await sqlite_store.put_file(
                FileMeta(
                    namespace_id="ns1",
                    path=p,
                    current_version_id="v1",
                    current_version_number=1,
                    created_at=now,
                    updated_at=now,
                )
            )
        results = await sqlite_store.list_dir("ns1", "/src/", recursive=True)
        paths = {r.path for r in results}
        assert paths == {"/src/a.py", "/src/b.py", "/src/sub/c.py"}

    @pytest.mark.asyncio
    async def test_put_version_first(self, sqlite_store):
        v = _version("ns1", "/a.py", 1)
        await sqlite_store.put_version(v, expected_version=None)
        f = await sqlite_store.get_file("ns1", "/a.py")
        assert f is not None
        assert f.current_version_number == 1

    @pytest.mark.asyncio
    async def test_put_version_cas_ok(self, sqlite_store):
        v1 = _version("ns1", "/a.py", 1)
        await sqlite_store.put_version(v1, expected_version=None)
        v2 = _version("ns1", "/a.py", 2)
        await sqlite_store.put_version(v2, expected_version=1)
        f = await sqlite_store.get_file("ns1", "/a.py")
        assert f.current_version_number == 2

    @pytest.mark.asyncio
    async def test_put_version_cas_conflict(self, sqlite_store):
        v1 = _version("ns1", "/a.py", 1)
        await sqlite_store.put_version(v1, expected_version=None)
        v2 = _version("ns1", "/a.py", 2)
        with pytest.raises(ConflictError):
            await sqlite_store.put_version(v2, expected_version=99)

    @pytest.mark.asyncio
    async def test_put_version_cas_conflict_no_orphan(self, sqlite_store):
        """Failed CAS must not leave an orphaned version row."""
        v1 = _version("ns1", "/a.py", 1)
        await sqlite_store.put_version(v1, expected_version=None)
        v2 = _version("ns1", "/a.py", 2)
        with pytest.raises(ConflictError):
            await sqlite_store.put_version(v2, expected_version=99)
        # Trigger an auto-commit via an unrelated operation to confirm the
        # failed insert was never staged.
        await sqlite_store.get_file("ns1", "/a.py")
        versions = await sqlite_store.list_versions("ns1", "/a.py")
        assert len(versions) == 1
        assert versions[0].version_number == 1

    @pytest.mark.asyncio
    async def test_list_dir_excludes_deleted(self, sqlite_store):
        """list_dir must not return files with is_deleted=True."""
        now = _now()
        await sqlite_store.put_file(
            FileMeta(
                namespace_id="ns1",
                path="/src/live.py",
                current_version_id="v1",
                current_version_number=1,
                created_at=now,
                updated_at=now,
            )
        )
        await sqlite_store.put_file(
            FileMeta(
                namespace_id="ns1",
                path="/src/gone.py",
                current_version_id="v2",
                current_version_number=2,
                created_at=now,
                updated_at=now,
                is_deleted=True,
            )
        )
        results = await sqlite_store.list_dir("ns1", "/src/")
        paths = {r.path for r in results}
        assert paths == {"/src/live.py"}

    @pytest.mark.asyncio
    async def test_get_version_latest(self, sqlite_store):
        v1 = _version("ns1", "/a.py", 1, content_hash="h1")
        v2 = _version("ns1", "/a.py", 2, content_hash="h2")
        await sqlite_store.put_version(v1, expected_version=None)
        await sqlite_store.put_version(v2, expected_version=1)
        latest = await sqlite_store.get_version("ns1", "/a.py")
        assert latest is not None
        assert latest.version_number == 2

    @pytest.mark.asyncio
    async def test_get_version_by_number(self, sqlite_store):
        v1 = _version("ns1", "/a.py", 1, content_hash="h1")
        v2 = _version("ns1", "/a.py", 2, content_hash="h2")
        await sqlite_store.put_version(v1, expected_version=None)
        await sqlite_store.put_version(v2, expected_version=1)
        result = await sqlite_store.get_version("ns1", "/a.py", 1)
        assert result.content_hash == "h1"

    @pytest.mark.asyncio
    async def test_list_versions(self, sqlite_store):
        for i in range(1, 4):
            v = _version("ns1", "/a.py", i, content_hash=f"h{i}")
            ev = None if i == 1 else i - 1
            await sqlite_store.put_version(v, expected_version=ev)
        versions = await sqlite_store.list_versions("ns1", "/a.py")
        assert len(versions) == 3
        assert versions[0].version_number == 3  # newest first

        # Test before cursor
        versions = await sqlite_store.list_versions("ns1", "/a.py", before=3)
        assert len(versions) == 2
        assert versions[0].version_number == 2


class TestPermissions:
    """Task 8: permissions."""

    @pytest.mark.asyncio
    async def test_check_permission_no_rules(self, sqlite_store):
        result = await sqlite_store.check_permission("p1", "ns1", "/any/path", "read")
        assert result is False

    @pytest.mark.asyncio
    async def test_check_permission_matching_prefix(self, sqlite_store):
        perm = Permission(
            id="perm1",
            principal_id="p1",
            namespace_id="ns1",
            path_prefix="/",
            operations={"read"},
            created_at=_now(),
        )
        await sqlite_store.set_permission(perm)
        assert await sqlite_store.check_permission("p1", "ns1", "/any/path", "read") is True

    @pytest.mark.asyncio
    async def test_check_permission_most_specific(self, sqlite_store):
        # Broad read on /
        await sqlite_store.set_permission(
            Permission(
                id="perm1",
                principal_id="p1",
                namespace_id="ns1",
                path_prefix="/",
                operations={"read"},
                created_at=_now(),
            )
        )
        # Narrow write on /workspace/
        await sqlite_store.set_permission(
            Permission(
                id="perm2",
                principal_id="p1",
                namespace_id="ns1",
                path_prefix="/workspace/",
                operations={"write"},
                created_at=_now(),
            )
        )
        # write on /workspace/file.txt → True (matches /workspace/ prefix)
        assert await sqlite_store.check_permission("p1", "ns1", "/workspace/file.txt", "write") is True
        # write on /other/ → False (matches / prefix, which only has read)
        assert await sqlite_store.check_permission("p1", "ns1", "/other/file.txt", "write") is False

    @pytest.mark.asyncio
    async def test_set_and_get_permission(self, sqlite_store):
        perm = Permission(
            id="perm1",
            principal_id="p1",
            namespace_id="ns1",
            path_prefix="/",
            operations={"read", "write"},
            created_at=_now(),
        )
        await sqlite_store.set_permission(perm)
        assert await sqlite_store.check_permission("p1", "ns1", "/file", "read") is True
        assert await sqlite_store.check_permission("p1", "ns1", "/file", "write") is True

    @pytest.mark.asyncio
    async def test_set_permission_replaces_same_scope(self, sqlite_store):
        """set_permission on an existing (principal, namespace, prefix) must update, not append."""
        await sqlite_store.set_permission(
            Permission(
                id="perm1",
                principal_id="p1",
                namespace_id="ns1",
                path_prefix="/",
                operations={"read"},
                created_at=_now(),
            )
        )
        # Same scope, different id — should replace, not add a second row
        await sqlite_store.set_permission(
            Permission(
                id="perm2",
                principal_id="p1",
                namespace_id="ns1",
                path_prefix="/",
                operations={"write"},
                created_at=_now(),
            )
        )
        rows = await sqlite_store._execute_fetchall(
            "SELECT id FROM permissions WHERE principal_id='p1' AND namespace_id='ns1' AND path_prefix='/'"
        )
        assert len(rows) == 1
        # The replacement operations take effect
        assert await sqlite_store.check_permission("p1", "ns1", "/file", "write") is True
        assert await sqlite_store.check_permission("p1", "ns1", "/file", "read") is False

    @pytest.mark.asyncio
    async def test_namespace_isolation(self, sqlite_store):
        perm = Permission(
            id="perm1",
            principal_id="p1",
            namespace_id="nsA",
            path_prefix="/",
            operations={"read"},
            created_at=_now(),
        )
        await sqlite_store.set_permission(perm)
        assert await sqlite_store.check_permission("p1", "nsA", "/file", "read") is True
        assert await sqlite_store.check_permission("p1", "nsB", "/file", "read") is False


class TestAuditSearchNamesGC:
    """Task 9: audit, search meta, names, GC."""

    @pytest.mark.asyncio
    async def test_append_audit_event(self, sqlite_store):
        event = AuditEvent(
            event_id="evt1",
            timestamp=_now(),
            namespace_id="ns1",
            principal_id="p1",
            operation="write",
            path="/a.py",
        )
        await sqlite_store.append_audit_event(event)
        # Append a second
        event2 = AuditEvent(
            event_id="evt2",
            timestamp=_now(),
            namespace_id="ns1",
            principal_id="p1",
            operation="delete",
            path="/b.py",
        )
        await sqlite_store.append_audit_event(event2)
        rows = await sqlite_store._execute_fetchall("SELECT event_id FROM audit_events WHERE namespace_id='ns1'")
        assert len(rows) == 2

    def test_audit_not_updatable(self, sqlite_store):
        assert not hasattr(sqlite_store, "update_audit_event")

    @pytest.mark.asyncio
    async def test_update_search_meta(self, sqlite_store):
        v = _version("ns1", "/a.py", 1)
        await sqlite_store.put_version(v, expected_version=None)
        await sqlite_store.update_search_meta(v.id, {"key": "value"})
        fetched = await sqlite_store.get_version("ns1", "/a.py", 1)
        assert fetched.search_meta == {"key": "value"}

    @pytest.mark.asyncio
    async def test_set_and_resolve_name(self, sqlite_store):
        await sqlite_store.set_name("namespace", "ulid123", "my-workspace")
        result = await sqlite_store.resolve_name("namespace", "my-workspace")
        assert result == "ulid123"

    @pytest.mark.asyncio
    async def test_resolve_name_missing(self, sqlite_store):
        result = await sqlite_store.resolve_name("namespace", "unknown")
        assert result is None

    @pytest.mark.asyncio
    async def test_list_reclaimable_versions(self, sqlite_store):
        for i in range(1, 4):
            v = _version("ns1", "/a.py", i, content_hash=f"h{i}")
            ev = None if i == 1 else i - 1
            await sqlite_store.put_version(v, expected_version=ev)
        policy = RetentionPolicy(max_recent_versions=1)
        reclaimable = await sqlite_store.list_reclaimable_versions(policy, "ns1")
        # version 1 kept (keep_first_version=True), version 3 kept (most recent)
        # version 2 is reclaimable
        assert len(reclaimable) == 1
        assert reclaimable[0].version_number == 2

    @pytest.mark.asyncio
    async def test_delete_versions(self, sqlite_store):
        v1 = _version("ns1", "/a.py", 1)
        v2 = _version("ns1", "/a.py", 2)
        await sqlite_store.put_version(v1, expected_version=None)
        await sqlite_store.put_version(v2, expected_version=1)
        await sqlite_store.delete_versions([v1.id])
        versions = await sqlite_store.list_versions("ns1", "/a.py")
        assert len(versions) == 1
        assert versions[0].id == v2.id

    @pytest.mark.asyncio
    async def test_conforms_to_protocol(self, sqlite_store):
        assert isinstance(sqlite_store, MetadataStore)
