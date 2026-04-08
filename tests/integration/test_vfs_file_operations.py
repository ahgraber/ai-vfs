"""Integration tests for VFS file operations (Tasks 14-17b)."""

from __future__ import annotations

import pytest

# --- Helpers ---


async def _setup_ns_principal(vfs):
    """Create a namespace, principal, and grant full permissions."""
    ns = await vfs.create_namespace("test-ws", "admin")
    p = await vfs.create_principal("agent-alice")
    await vfs.grant(p.id, ns.id, "/", {"read", "write", "delete"})
    return ns, p


# --- Task 14: stat and list ---


class TestVFSStat:
    @pytest.mark.asyncio
    async def test_stat_returns_metadata(self, vfs_instance):
        ns, p = await _setup_ns_principal(vfs_instance)
        await vfs_instance.write(ns.id, "/src/main.py", b"hello", principal_id=p.id)
        meta = await vfs_instance.stat(ns.id, "/src/main.py", principal_id=p.id)
        assert meta.path == "/src/main.py"
        assert meta.current_version_number == 1

    @pytest.mark.asyncio
    async def test_stat_not_found(self, vfs_instance):
        ns, p = await _setup_ns_principal(vfs_instance)
        from vfs.errors import NotFoundError

        with pytest.raises(NotFoundError):
            await vfs_instance.stat(ns.id, "/nonexistent", principal_id=p.id)

    @pytest.mark.asyncio
    async def test_stat_permission_denied(self, vfs_instance):
        ns, p = await _setup_ns_principal(vfs_instance)
        await vfs_instance.write(ns.id, "/file", b"data", principal_id=p.id)
        no_perm = await vfs_instance.create_principal("no-perms")
        from vfs.errors import PermissionDeniedError

        with pytest.raises(PermissionDeniedError):
            await vfs_instance.stat(ns.id, "/file", principal_id=no_perm.id)


class TestVFSList:
    @pytest.mark.asyncio
    async def test_list_non_recursive(self, vfs_instance):
        ns, p = await _setup_ns_principal(vfs_instance)
        for path in ["/src/a.py", "/src/b.py", "/src/c.py", "/src/sub/d.py"]:
            await vfs_instance.write(ns.id, path, b"x", principal_id=p.id)
        results = await vfs_instance.list(ns.id, "/src/", principal_id=p.id, recursive=False)
        paths = {r.path for r in results}
        assert paths == {"/src/a.py", "/src/b.py", "/src/c.py"}

    @pytest.mark.asyncio
    async def test_list_recursive(self, vfs_instance):
        ns, p = await _setup_ns_principal(vfs_instance)
        for path in ["/src/a.py", "/src/b.py", "/src/sub/c.py"]:
            await vfs_instance.write(ns.id, path, b"x", principal_id=p.id)
        results = await vfs_instance.list(ns.id, "/src/", principal_id=p.id, recursive=True)
        paths = {r.path for r in results}
        assert paths == {"/src/a.py", "/src/b.py", "/src/sub/c.py"}

    @pytest.mark.asyncio
    async def test_list_invisible_pruning(self, vfs_instance):
        ns, p = await _setup_ns_principal(vfs_instance)
        await vfs_instance.write(ns.id, "/public/a.py", b"x", principal_id=p.id)
        await vfs_instance.write(ns.id, "/secret/b.py", b"x", principal_id=p.id)
        # Create limited principal with read on /public/ only
        limited = await vfs_instance.create_principal("limited")
        await vfs_instance.grant(limited.id, ns.id, "/public/", {"read"})
        results = await vfs_instance.list(ns.id, "/", principal_id=limited.id, recursive=True)
        paths = {r.path for r in results}
        assert paths == {"/public/a.py"}

    @pytest.mark.asyncio
    async def test_list_namespace_isolation(self, vfs_instance):
        ns_a = await vfs_instance.create_namespace("ws-a", "admin")
        ns_b = await vfs_instance.create_namespace("ws-b", "admin")
        p = await vfs_instance.create_principal("agent")
        await vfs_instance.grant(p.id, ns_a.id, "/", {"read", "write"})
        await vfs_instance.grant(p.id, ns_b.id, "/", {"read", "write"})
        await vfs_instance.write(ns_a.id, "/a.py", b"a", principal_id=p.id)
        await vfs_instance.write(ns_b.id, "/b.py", b"b", principal_id=p.id)
        results_a = await vfs_instance.list(ns_a.id, "/", principal_id=p.id, recursive=True)
        assert len(results_a) == 1
        assert results_a[0].path == "/a.py"


# --- Task 15: write ---


class TestVFSWrite:
    @pytest.mark.asyncio
    async def test_write_returns_version_meta(self, vfs_instance):
        ns, p = await _setup_ns_principal(vfs_instance)
        ver = await vfs_instance.write(ns.id, "/a.py", b"hello", principal_id=p.id)
        assert ver.version_number == 1
        assert ver.file_path == "/a.py"

    @pytest.mark.asyncio
    async def test_write_creates_new_version(self, vfs_instance):
        ns, p = await _setup_ns_principal(vfs_instance)
        v1 = await vfs_instance.write(ns.id, "/a.py", b"v1", principal_id=p.id)
        v2 = await vfs_instance.write(ns.id, "/a.py", b"v2", principal_id=p.id)
        assert v1.version_number == 1
        assert v2.version_number == 2

    @pytest.mark.asyncio
    async def test_write_content_addressed(self, vfs_instance):
        ns, p = await _setup_ns_principal(vfs_instance)
        v1 = await vfs_instance.write(ns.id, "/a.py", b"same", principal_id=p.id)
        v2 = await vfs_instance.write(ns.id, "/b.py", b"same", principal_id=p.id)
        assert v1.content_hash == v2.content_hash

    @pytest.mark.asyncio
    async def test_write_cas_conflict(self, vfs_instance):
        ns, p = await _setup_ns_principal(vfs_instance)
        await vfs_instance.write(ns.id, "/a.py", b"v1", principal_id=p.id)
        from vfs.errors import ConflictError

        with pytest.raises(ConflictError):
            await vfs_instance.write(ns.id, "/a.py", b"v2", principal_id=p.id, expected_version=99)

    @pytest.mark.asyncio
    async def test_write_permission_denied(self, vfs_instance):
        ns, p = await _setup_ns_principal(vfs_instance)
        no_perm = await vfs_instance.create_principal("no-write")
        from vfs.errors import PermissionDeniedError

        with pytest.raises(PermissionDeniedError):
            await vfs_instance.write(ns.id, "/a.py", b"x", principal_id=no_perm.id)

    @pytest.mark.asyncio
    async def test_write_creates_audit_event(self, vfs_instance):
        ns, p = await _setup_ns_principal(vfs_instance)
        await vfs_instance.write(ns.id, "/a.py", b"x", principal_id=p.id)
        rows = await vfs_instance._meta._execute_fetchall(
            "SELECT operation FROM audit_events WHERE namespace_id=?", (ns.id,)
        )
        ops = [r[0] for r in rows]
        assert "write" in ops

    @pytest.mark.asyncio
    async def test_write_updates_search_meta(self, vfs_instance):
        """Search provider returning non-empty dict should populate version.search_meta."""
        ns, p = await _setup_ns_principal(vfs_instance)

        class MockSearchProvider:
            async def index(self, path, content, metadata):
                return {"test_key": "test_value"}

            async def search(self, query, scope, search_type, candidates, fetch_content=None):
                return []

            def capabilities(self):
                from vfs.models import SearchType

                return {SearchType.GLOB}

        vfs_instance._search = MockSearchProvider()
        ver = await vfs_instance.write(ns.id, "/a.py", b"content", principal_id=p.id)
        assert ver.search_meta == {"test_key": "test_value"}
        # Verify persisted in DB
        stored = await vfs_instance._meta.get_version(ns.id, "/a.py")
        assert stored.search_meta == {"test_key": "test_value"}


# --- Task 16: read ---


class TestVFSRead:
    @pytest.mark.asyncio
    async def test_read_returns_content(self, vfs_instance):
        ns, p = await _setup_ns_principal(vfs_instance)
        await vfs_instance.write(ns.id, "/a.py", b"hello world", principal_id=p.id)
        content = await vfs_instance.read(ns.id, "/a.py", principal_id=p.id)
        assert content == b"hello world"

    @pytest.mark.asyncio
    async def test_read_specific_version(self, vfs_instance):
        ns, p = await _setup_ns_principal(vfs_instance)
        await vfs_instance.write(ns.id, "/a.py", b"first", principal_id=p.id)
        await vfs_instance.write(ns.id, "/a.py", b"second", principal_id=p.id)
        content = await vfs_instance.read(ns.id, "/a.py", principal_id=p.id, version_number=1)
        assert content == b"first"

    @pytest.mark.asyncio
    async def test_read_deleted_file(self, vfs_instance):
        ns, p = await _setup_ns_principal(vfs_instance)
        await vfs_instance.write(ns.id, "/a.py", b"data", principal_id=p.id)
        await vfs_instance.delete(ns.id, "/a.py", principal_id=p.id)
        from vfs.errors import NotFoundError

        with pytest.raises(NotFoundError):
            await vfs_instance.read(ns.id, "/a.py", principal_id=p.id)

    @pytest.mark.asyncio
    async def test_read_permission_denied(self, vfs_instance):
        ns, p = await _setup_ns_principal(vfs_instance)
        await vfs_instance.write(ns.id, "/a.py", b"data", principal_id=p.id)
        no_perm = await vfs_instance.create_principal("no-read")
        from vfs.errors import PermissionDeniedError

        with pytest.raises(PermissionDeniedError):
            await vfs_instance.read(ns.id, "/a.py", principal_id=no_perm.id)

    @pytest.mark.asyncio
    async def test_read_not_audited(self, vfs_instance):
        ns, p = await _setup_ns_principal(vfs_instance)
        await vfs_instance.write(ns.id, "/a.py", b"data", principal_id=p.id)
        # Clear audit events from write
        await vfs_instance._meta._conn.execute("DELETE FROM audit_events WHERE namespace_id=?", (ns.id,))
        await vfs_instance._meta._conn.commit()
        # Read should not create audit
        await vfs_instance.read(ns.id, "/a.py", principal_id=p.id)
        rows = await vfs_instance._meta._execute_fetchall("SELECT * FROM audit_events WHERE namespace_id=?", (ns.id,))
        assert len(rows) == 0


# --- Task 17: delete ---


class TestVFSDelete:
    @pytest.mark.asyncio
    async def test_delete_creates_tombstone(self, vfs_instance):
        ns, p = await _setup_ns_principal(vfs_instance)
        await vfs_instance.write(ns.id, "/a.py", b"data", principal_id=p.id)
        await vfs_instance.delete(ns.id, "/a.py", principal_id=p.id)
        meta = await vfs_instance.stat(ns.id, "/a.py", principal_id=p.id)
        assert meta.is_deleted is True

    @pytest.mark.asyncio
    async def test_delete_old_versions_still_accessible(self, vfs_instance):
        ns, p = await _setup_ns_principal(vfs_instance)
        await vfs_instance.write(ns.id, "/a.py", b"v1 content", principal_id=p.id)
        await vfs_instance.delete(ns.id, "/a.py", principal_id=p.id)
        # Version 1 should still be readable
        content = await vfs_instance.read(ns.id, "/a.py", principal_id=p.id, version_number=1)
        assert content == b"v1 content"

    @pytest.mark.asyncio
    async def test_delete_permission_denied(self, vfs_instance):
        ns, p = await _setup_ns_principal(vfs_instance)
        await vfs_instance.write(ns.id, "/a.py", b"data", principal_id=p.id)
        read_only = await vfs_instance.create_principal("reader")
        await vfs_instance.grant(read_only.id, ns.id, "/", {"read"})
        from vfs.errors import PermissionDeniedError

        with pytest.raises(PermissionDeniedError):
            await vfs_instance.delete(ns.id, "/a.py", principal_id=read_only.id)

    @pytest.mark.asyncio
    async def test_delete_creates_audit_event(self, vfs_instance):
        ns, p = await _setup_ns_principal(vfs_instance)
        await vfs_instance.write(ns.id, "/a.py", b"data", principal_id=p.id)
        await vfs_instance.delete(ns.id, "/a.py", principal_id=p.id)
        rows = await vfs_instance._meta._execute_fetchall(
            "SELECT operation FROM audit_events WHERE namespace_id=?", (ns.id,)
        )
        ops = [r[0] for r in rows]
        assert "delete" in ops


# --- Task 17b: copy and move ---


class TestVFSCopy:
    @pytest.mark.asyncio
    async def test_copy_to_new_path(self, vfs_instance):
        ns, p = await _setup_ns_principal(vfs_instance)
        await vfs_instance.write(ns.id, "/src/a.py", b"content", principal_id=p.id)
        ver = await vfs_instance.copy(ns.id, "/src/a.py", "/dst/a.py", principal_id=p.id)
        assert ver.version_number == 1
        # Source unchanged
        src = await vfs_instance.stat(ns.id, "/src/a.py", principal_id=p.id)
        assert src.current_version_number == 1
        # Dest readable
        content = await vfs_instance.read(ns.id, "/dst/a.py", principal_id=p.id)
        assert content == b"content"

    @pytest.mark.asyncio
    async def test_copy_to_existing_path(self, vfs_instance):
        ns, p = await _setup_ns_principal(vfs_instance)
        await vfs_instance.write(ns.id, "/a.py", b"original", principal_id=p.id)
        await vfs_instance.write(ns.id, "/b.py", b"source", principal_id=p.id)
        ver = await vfs_instance.copy(ns.id, "/b.py", "/a.py", principal_id=p.id)
        assert ver.version_number == 2

    @pytest.mark.asyncio
    async def test_copy_nonexistent_source(self, vfs_instance):
        ns, p = await _setup_ns_principal(vfs_instance)
        from vfs.errors import NotFoundError

        with pytest.raises(NotFoundError):
            await vfs_instance.copy(ns.id, "/nope", "/dst", principal_id=p.id)

    @pytest.mark.asyncio
    async def test_copy_no_blob_duplication(self, vfs_instance):
        ns, p = await _setup_ns_principal(vfs_instance)
        v1 = await vfs_instance.write(ns.id, "/a.py", b"data", principal_id=p.id)
        v2 = await vfs_instance.copy(ns.id, "/a.py", "/b.py", principal_id=p.id)
        assert v1.content_hash == v2.content_hash

    @pytest.mark.asyncio
    async def test_copy_permission_denied(self, vfs_instance):
        ns, p = await _setup_ns_principal(vfs_instance)
        await vfs_instance.write(ns.id, "/a.py", b"data", principal_id=p.id)
        no_perm = await vfs_instance.create_principal("no-copy")
        from vfs.errors import PermissionDeniedError

        with pytest.raises(PermissionDeniedError):
            await vfs_instance.copy(ns.id, "/a.py", "/b.py", principal_id=no_perm.id)

    @pytest.mark.asyncio
    async def test_copy_creates_audit_event(self, vfs_instance):
        ns, p = await _setup_ns_principal(vfs_instance)
        await vfs_instance.write(ns.id, "/a.py", b"data", principal_id=p.id)
        await vfs_instance.copy(ns.id, "/a.py", "/b.py", principal_id=p.id)
        rows = await vfs_instance._meta._execute_fetchall(
            "SELECT operation FROM audit_events WHERE namespace_id=?", (ns.id,)
        )
        ops = [r[0] for r in rows]
        assert "copy" in ops


class TestVFSMove:
    @pytest.mark.asyncio
    async def test_move_to_new_path(self, vfs_instance):
        ns, p = await _setup_ns_principal(vfs_instance)
        await vfs_instance.write(ns.id, "/src/a.py", b"content", principal_id=p.id)
        ver = await vfs_instance.move(ns.id, "/src/a.py", "/dst/a.py", principal_id=p.id)
        assert ver.version_number == 1
        # Source is tombstoned
        src = await vfs_instance.stat(ns.id, "/src/a.py", principal_id=p.id)
        assert src.is_deleted is True
        # Dest readable
        content = await vfs_instance.read(ns.id, "/dst/a.py", principal_id=p.id)
        assert content == b"content"

    @pytest.mark.asyncio
    async def test_move_to_existing_path(self, vfs_instance):
        ns, p = await _setup_ns_principal(vfs_instance)
        await vfs_instance.write(ns.id, "/a.py", b"orig", principal_id=p.id)
        await vfs_instance.write(ns.id, "/b.py", b"moved", principal_id=p.id)
        await vfs_instance.move(ns.id, "/b.py", "/a.py", principal_id=p.id)
        content = await vfs_instance.read(ns.id, "/a.py", principal_id=p.id)
        assert content == b"moved"

    @pytest.mark.asyncio
    async def test_move_nonexistent_source(self, vfs_instance):
        ns, p = await _setup_ns_principal(vfs_instance)
        from vfs.errors import NotFoundError

        with pytest.raises(NotFoundError):
            await vfs_instance.move(ns.id, "/nope", "/dst", principal_id=p.id)

    @pytest.mark.asyncio
    async def test_move_permission_denied(self, vfs_instance):
        ns, p = await _setup_ns_principal(vfs_instance)
        await vfs_instance.write(ns.id, "/a.py", b"data", principal_id=p.id)
        read_only = await vfs_instance.create_principal("reader")
        await vfs_instance.grant(read_only.id, ns.id, "/", {"read"})
        from vfs.errors import PermissionDeniedError

        with pytest.raises(PermissionDeniedError):
            await vfs_instance.move(ns.id, "/a.py", "/b.py", principal_id=read_only.id)

    @pytest.mark.asyncio
    async def test_move_atomicity(self, vfs_instance):
        """If dst creation fails mid-move, src must NOT be tombstoned (transaction rollback)."""
        ns, p = await _setup_ns_principal(vfs_instance)
        await vfs_instance.write(ns.id, "/src.py", b"data", principal_id=p.id)

        # Sabotage: create a version at dst with the same version_number that
        # the move would try to write, causing a UNIQUE constraint failure inside
        # the transaction.
        from datetime import datetime, timezone

        from ulid import ULID

        from vfs.models import VersionMeta

        dst_ver = VersionMeta(
            id=str(ULID()),
            file_path="/dst.py",
            namespace_id=ns.id,
            version_number=1,
            content_hash="fake",
            size=0,
            created_at=datetime.now(timezone.utc),
            created_by=p.id,
        )
        await vfs_instance._meta.put_version(dst_ver, expected_version=None)

        # Now attempt to move — the dst put_version will compute version_number=2
        # but we need to force a conflict. Instead, we'll monkeypatch to raise mid-transaction.
        original_put = vfs_instance._meta.put_version
        call_count = 0

        async def failing_put(version, *, expected_version=None):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("Simulated dst failure")
            return await original_put(version, expected_version=expected_version)

        vfs_instance._meta.put_version = failing_put

        with pytest.raises(RuntimeError, match="Simulated dst failure"):
            await vfs_instance.move(ns.id, "/src.py", "/dst.py", principal_id=p.id)

        # Source must NOT be tombstoned — transaction should have rolled back
        src = await vfs_instance.stat(ns.id, "/src.py", principal_id=p.id)
        assert src.is_deleted is False
        assert src.current_version_number == 1

    @pytest.mark.asyncio
    async def test_move_creates_audit_event(self, vfs_instance):
        ns, p = await _setup_ns_principal(vfs_instance)
        await vfs_instance.write(ns.id, "/a.py", b"data", principal_id=p.id)
        await vfs_instance.move(ns.id, "/a.py", "/b.py", principal_id=p.id)
        rows = await vfs_instance._meta._execute_fetchall(
            "SELECT operation FROM audit_events WHERE namespace_id=?", (ns.id,)
        )
        ops = [r[0] for r in rows]
        assert "move" in ops
