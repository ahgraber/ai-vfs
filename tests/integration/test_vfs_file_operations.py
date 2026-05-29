"""Integration tests for VFS file operations (Tasks 14-17b)."""

from __future__ import annotations

import pytest

# --- Helpers ---


async def _setup_ns_principal(vfs):
    """Create a namespace, bootstrap an admin, and grant a worker principal full operations.

    Returns (namespace, agent_principal, admin_principal). Existing callers that ignore
    the admin handle can unpack as `ns, p, _ = ...` or `ns, p, admin = ...`.
    """
    ns = await vfs.create_namespace("test-ws", "admin")
    admin = await vfs.create_principal("test-admin")
    await vfs.bootstrap_admin(admin.id, ns.id)
    p = await vfs.create_principal("agent-alice")
    await vfs.grant(admin.id, p.id, ns.id, "/", {"read", "write", "delete"})
    return ns, p, admin


# --- Task 14: stat and list ---


class TestVFSStat:
    @pytest.mark.asyncio
    async def test_stat_returns_metadata(self, vfs_instance):
        ns, p, admin = await _setup_ns_principal(vfs_instance)
        await vfs_instance.write(ns.id, "/src/main.py", b"hello", principal_id=p.id)
        meta = await vfs_instance.stat(ns.id, "/src/main.py", principal_id=p.id)
        assert meta.path == "/src/main.py"
        assert meta.current_version_number == 1

    @pytest.mark.asyncio
    async def test_stat_not_found(self, vfs_instance):
        ns, p, admin = await _setup_ns_principal(vfs_instance)
        from vfs.errors import NotFoundError

        with pytest.raises(NotFoundError):
            await vfs_instance.stat(ns.id, "/nonexistent", principal_id=p.id)

    @pytest.mark.asyncio
    async def test_stat_permission_denied(self, vfs_instance):
        ns, p, admin = await _setup_ns_principal(vfs_instance)
        await vfs_instance.write(ns.id, "/file", b"data", principal_id=p.id)
        no_perm = await vfs_instance.create_principal("no-perms")
        from vfs.errors import PermissionDeniedError

        with pytest.raises(PermissionDeniedError):
            await vfs_instance.stat(ns.id, "/file", principal_id=no_perm.id)


class TestVFSList:
    @pytest.mark.asyncio
    async def test_list_non_recursive(self, vfs_instance):
        ns, p, admin = await _setup_ns_principal(vfs_instance)
        for path in ["/src/a.py", "/src/b.py", "/src/c.py", "/src/sub/d.py"]:
            await vfs_instance.write(ns.id, path, b"x", principal_id=p.id)
        results = await vfs_instance.list(ns.id, "/src/", principal_id=p.id, recursive=False)
        paths = {r.path for r in results}
        assert paths == {"/src/a.py", "/src/b.py", "/src/c.py"}

    @pytest.mark.asyncio
    async def test_list_recursive(self, vfs_instance):
        ns, p, admin = await _setup_ns_principal(vfs_instance)
        for path in ["/src/a.py", "/src/b.py", "/src/sub/c.py"]:
            await vfs_instance.write(ns.id, path, b"x", principal_id=p.id)
        results = await vfs_instance.list(ns.id, "/src/", principal_id=p.id, recursive=True)
        paths = {r.path for r in results}
        assert paths == {"/src/a.py", "/src/b.py", "/src/sub/c.py"}

    @pytest.mark.asyncio
    async def test_list_invisible_pruning(self, vfs_instance):
        ns, p, admin = await _setup_ns_principal(vfs_instance)
        await vfs_instance.write(ns.id, "/public/a.py", b"x", principal_id=p.id)
        await vfs_instance.write(ns.id, "/secret/b.py", b"x", principal_id=p.id)
        # Create limited principal with read on /public/ only
        limited = await vfs_instance.create_principal("limited")
        await vfs_instance.grant(admin.id, limited.id, ns.id, "/public/", {"read"})
        results = await vfs_instance.list(ns.id, "/", principal_id=limited.id, recursive=True)
        paths = {r.path for r in results}
        assert paths == {"/public/a.py"}

    @pytest.mark.asyncio
    async def test_list_namespace_isolation(self, vfs_instance):
        ns_a = await vfs_instance.create_namespace("ws-a", "admin")
        ns_b = await vfs_instance.create_namespace("ws-b", "admin")
        admin_a = await vfs_instance.create_principal("admin-a")
        admin_b = await vfs_instance.create_principal("admin-b")
        await vfs_instance.bootstrap_admin(admin_a.id, ns_a.id)
        await vfs_instance.bootstrap_admin(admin_b.id, ns_b.id)
        p = await vfs_instance.create_principal("agent")
        await vfs_instance.grant(admin_a.id, p.id, ns_a.id, "/", {"read", "write"})
        await vfs_instance.grant(admin_b.id, p.id, ns_b.id, "/", {"read", "write"})
        await vfs_instance.write(ns_a.id, "/a.py", b"a", principal_id=p.id)
        await vfs_instance.write(ns_b.id, "/b.py", b"b", principal_id=p.id)
        results_a = await vfs_instance.list(ns_a.id, "/", principal_id=p.id, recursive=True)
        assert len(results_a) == 1
        assert results_a[0].path == "/a.py"


# --- Task 15: write ---


class TestVFSWrite:
    @pytest.mark.asyncio
    async def test_write_returns_version_meta(self, vfs_instance):
        ns, p, admin = await _setup_ns_principal(vfs_instance)
        ver = await vfs_instance.write(ns.id, "/a.py", b"hello", principal_id=p.id)
        assert ver.version_number == 1
        assert ver.file_path == "/a.py"

    @pytest.mark.asyncio
    async def test_write_creates_new_version(self, vfs_instance):
        ns, p, admin = await _setup_ns_principal(vfs_instance)
        v1 = await vfs_instance.write(ns.id, "/a.py", b"v1", principal_id=p.id)
        v2 = await vfs_instance.write(ns.id, "/a.py", b"v2", principal_id=p.id)
        assert v1.version_number == 1
        assert v2.version_number == 2

    @pytest.mark.asyncio
    async def test_write_content_addressed(self, vfs_instance):
        ns, p, admin = await _setup_ns_principal(vfs_instance)
        v1 = await vfs_instance.write(ns.id, "/a.py", b"same", principal_id=p.id)
        v2 = await vfs_instance.write(ns.id, "/b.py", b"same", principal_id=p.id)
        assert v1.content_hash == v2.content_hash

    @pytest.mark.asyncio
    async def test_write_cas_conflict(self, vfs_instance):
        ns, p, admin = await _setup_ns_principal(vfs_instance)
        await vfs_instance.write(ns.id, "/a.py", b"v1", principal_id=p.id)
        from vfs.errors import ConflictError

        with pytest.raises(ConflictError):
            await vfs_instance.write(ns.id, "/a.py", b"v2", principal_id=p.id, expected_version=99)

    @pytest.mark.asyncio
    async def test_write_permission_denied(self, vfs_instance):
        ns, p, admin = await _setup_ns_principal(vfs_instance)
        no_perm = await vfs_instance.create_principal("no-write")
        from vfs.errors import PermissionDeniedError

        with pytest.raises(PermissionDeniedError):
            await vfs_instance.write(ns.id, "/a.py", b"x", principal_id=no_perm.id)

    @pytest.mark.asyncio
    async def test_write_creates_audit_event(self, vfs_instance):
        ns, p, admin = await _setup_ns_principal(vfs_instance)
        await vfs_instance.write(ns.id, "/a.py", b"x", principal_id=p.id)
        rows = await vfs_instance._meta._execute_fetchall(
            "SELECT operation FROM audit_events WHERE namespace_id=?", (ns.id,)
        )
        ops = [r[0] for r in rows]
        assert "write" in ops

    @pytest.mark.asyncio
    async def test_write_updates_search_meta(self, vfs_instance):
        """Search provider returning non-empty dict should populate version.search_meta."""
        ns, p, admin = await _setup_ns_principal(vfs_instance)

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
        ns, p, admin = await _setup_ns_principal(vfs_instance)
        await vfs_instance.write(ns.id, "/a.py", b"hello world", principal_id=p.id)
        content = await vfs_instance.read(ns.id, "/a.py", principal_id=p.id)
        assert content == b"hello world"

    @pytest.mark.asyncio
    async def test_read_specific_version(self, vfs_instance):
        ns, p, admin = await _setup_ns_principal(vfs_instance)
        await vfs_instance.write(ns.id, "/a.py", b"first", principal_id=p.id)
        await vfs_instance.write(ns.id, "/a.py", b"second", principal_id=p.id)
        content = await vfs_instance.read(ns.id, "/a.py", principal_id=p.id, version_number=1)
        assert content == b"first"

    @pytest.mark.asyncio
    async def test_read_deleted_file(self, vfs_instance):
        ns, p, admin = await _setup_ns_principal(vfs_instance)
        await vfs_instance.write(ns.id, "/a.py", b"data", principal_id=p.id)
        await vfs_instance.delete(ns.id, "/a.py", principal_id=p.id)
        from vfs.errors import NotFoundError

        with pytest.raises(NotFoundError):
            await vfs_instance.read(ns.id, "/a.py", principal_id=p.id)

    @pytest.mark.asyncio
    async def test_read_permission_denied(self, vfs_instance):
        ns, p, admin = await _setup_ns_principal(vfs_instance)
        await vfs_instance.write(ns.id, "/a.py", b"data", principal_id=p.id)
        no_perm = await vfs_instance.create_principal("no-read")
        from vfs.errors import PermissionDeniedError

        with pytest.raises(PermissionDeniedError):
            await vfs_instance.read(ns.id, "/a.py", principal_id=no_perm.id)

    @pytest.mark.asyncio
    async def test_read_not_audited(self, vfs_instance):
        ns, p, admin = await _setup_ns_principal(vfs_instance)
        await vfs_instance.write(ns.id, "/a.py", b"data", principal_id=p.id)
        # Clear audit events from write
        await vfs_instance._meta._conn.exec_driver_sql("DELETE FROM audit_events WHERE namespace_id=?", (ns.id,))
        await vfs_instance._meta._conn.commit()
        # Read should not create audit
        await vfs_instance.read(ns.id, "/a.py", principal_id=p.id)
        rows = await vfs_instance._meta._execute_fetchall("SELECT * FROM audit_events WHERE namespace_id=?", (ns.id,))
        assert len(rows) == 0


# --- Task 17: delete ---


class TestVFSDelete:
    @pytest.mark.asyncio
    async def test_delete_creates_tombstone(self, vfs_instance):
        ns, p, admin = await _setup_ns_principal(vfs_instance)
        await vfs_instance.write(ns.id, "/a.py", b"data", principal_id=p.id)
        await vfs_instance.delete(ns.id, "/a.py", principal_id=p.id)
        meta = await vfs_instance.stat(ns.id, "/a.py", principal_id=p.id)
        assert meta.is_deleted is True

    @pytest.mark.asyncio
    async def test_delete_old_versions_still_accessible(self, vfs_instance):
        ns, p, admin = await _setup_ns_principal(vfs_instance)
        await vfs_instance.write(ns.id, "/a.py", b"v1 content", principal_id=p.id)
        await vfs_instance.delete(ns.id, "/a.py", principal_id=p.id)
        # Version 1 should still be readable
        content = await vfs_instance.read(ns.id, "/a.py", principal_id=p.id, version_number=1)
        assert content == b"v1 content"

    @pytest.mark.asyncio
    async def test_delete_permission_denied(self, vfs_instance):
        ns, p, admin = await _setup_ns_principal(vfs_instance)
        await vfs_instance.write(ns.id, "/a.py", b"data", principal_id=p.id)
        read_only = await vfs_instance.create_principal("reader")
        await vfs_instance.grant(admin.id, read_only.id, ns.id, "/", {"read"})
        from vfs.errors import PermissionDeniedError

        with pytest.raises(PermissionDeniedError):
            await vfs_instance.delete(ns.id, "/a.py", principal_id=read_only.id)

    @pytest.mark.asyncio
    async def test_delete_creates_audit_event(self, vfs_instance):
        ns, p, admin = await _setup_ns_principal(vfs_instance)
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
        ns, p, admin = await _setup_ns_principal(vfs_instance)
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
        ns, p, admin = await _setup_ns_principal(vfs_instance)
        await vfs_instance.write(ns.id, "/a.py", b"original", principal_id=p.id)
        await vfs_instance.write(ns.id, "/b.py", b"source", principal_id=p.id)
        ver = await vfs_instance.copy(ns.id, "/b.py", "/a.py", principal_id=p.id)
        assert ver.version_number == 2

    @pytest.mark.asyncio
    async def test_copy_nonexistent_source(self, vfs_instance):
        ns, p, admin = await _setup_ns_principal(vfs_instance)
        from vfs.errors import NotFoundError

        with pytest.raises(NotFoundError):
            await vfs_instance.copy(ns.id, "/nope", "/dst", principal_id=p.id)

    @pytest.mark.asyncio
    async def test_copy_no_blob_duplication(self, vfs_instance):
        ns, p, admin = await _setup_ns_principal(vfs_instance)
        v1 = await vfs_instance.write(ns.id, "/a.py", b"data", principal_id=p.id)
        v2 = await vfs_instance.copy(ns.id, "/a.py", "/b.py", principal_id=p.id)
        assert v1.content_hash == v2.content_hash

    @pytest.mark.asyncio
    async def test_copy_permission_denied(self, vfs_instance):
        ns, p, admin = await _setup_ns_principal(vfs_instance)
        await vfs_instance.write(ns.id, "/a.py", b"data", principal_id=p.id)
        no_perm = await vfs_instance.create_principal("no-copy")
        from vfs.errors import PermissionDeniedError

        with pytest.raises(PermissionDeniedError):
            await vfs_instance.copy(ns.id, "/a.py", "/b.py", principal_id=no_perm.id)

    @pytest.mark.asyncio
    async def test_copy_creates_audit_event(self, vfs_instance):
        ns, p, admin = await _setup_ns_principal(vfs_instance)
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
        ns, p, admin = await _setup_ns_principal(vfs_instance)
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
        ns, p, admin = await _setup_ns_principal(vfs_instance)
        await vfs_instance.write(ns.id, "/a.py", b"orig", principal_id=p.id)
        await vfs_instance.write(ns.id, "/b.py", b"moved", principal_id=p.id)
        await vfs_instance.move(ns.id, "/b.py", "/a.py", principal_id=p.id)
        content = await vfs_instance.read(ns.id, "/a.py", principal_id=p.id)
        assert content == b"moved"

    @pytest.mark.asyncio
    async def test_move_nonexistent_source(self, vfs_instance):
        ns, p, admin = await _setup_ns_principal(vfs_instance)
        from vfs.errors import NotFoundError

        with pytest.raises(NotFoundError):
            await vfs_instance.move(ns.id, "/nope", "/dst", principal_id=p.id)

    @pytest.mark.asyncio
    async def test_move_permission_denied(self, vfs_instance):
        ns, p, admin = await _setup_ns_principal(vfs_instance)
        await vfs_instance.write(ns.id, "/a.py", b"data", principal_id=p.id)
        read_only = await vfs_instance.create_principal("reader")
        await vfs_instance.grant(admin.id, read_only.id, ns.id, "/", {"read"})
        from vfs.errors import PermissionDeniedError

        with pytest.raises(PermissionDeniedError):
            await vfs_instance.move(ns.id, "/a.py", "/b.py", principal_id=read_only.id)

    @pytest.mark.asyncio
    async def test_move_atomic_on_transactional_store(self, vfs_instance):
        """MoveAtomicOnTransactionalStore: on the default SQLite store (real transaction), a
        failure on the 2nd put_version (the source tombstone, issued after the destination
        write) must roll back the whole block — neither a partial destination nor an
        unintended source tombstone is observable."""
        from vfs.errors import NotFoundError

        ns, p, admin = await _setup_ns_principal(vfs_instance)
        await vfs_instance.write(ns.id, "/src.py", b"data", principal_id=p.id)

        # After the destination-before-source reorder, the 2nd put_version is the source
        # tombstone. Inject a failure there; the SQLite transaction must roll back the
        # already-issued destination write too.
        original_put = vfs_instance._meta.put_version
        call_count = 0

        async def failing_put(version, *, expected_version=None):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("Simulated tombstone failure")
            return await original_put(version, expected_version=expected_version)

        vfs_instance._meta.put_version = failing_put

        with pytest.raises(RuntimeError, match="Simulated tombstone failure"):
            await vfs_instance.move(ns.id, "/src.py", "/dst.py", principal_id=p.id)

        vfs_instance._meta.put_version = original_put

        # Source must NOT be tombstoned — the transaction rolled back.
        src = await vfs_instance.stat(ns.id, "/src.py", principal_id=p.id)
        assert src.is_deleted is False
        assert src.current_version_number == 1
        # Destination must NOT have been created — the already-issued dst write rolled back.
        assert await vfs_instance._meta.get_file(ns.id, "/dst.py") is None
        with pytest.raises(NotFoundError):
            await vfs_instance.stat(ns.id, "/dst.py", principal_id=p.id)

    @pytest.mark.asyncio
    async def test_move_non_destructive_on_best_effort_store(self, vfs_instance):
        """MoveNonDestructiveOnBestEffortStore: with a best-effort no-op transaction(), each
        put_version commits on its own. A failure on the 2nd put_version (the source
        tombstone, after the destination is created) must leave the file readable at BOTH
        source and destination — no version is lost."""
        from contextlib import asynccontextmanager

        ns, p, admin = await _setup_ns_principal(vfs_instance)
        await vfs_instance.write(ns.id, "/src.py", b"data", principal_id=p.id)

        # Simulate a best-effort store: transaction() becomes a no-op, so each put_version
        # commits via the store's own per-operation boundary rather than as one unit.
        @asynccontextmanager
        async def _noop_txn():
            yield

        vfs_instance._meta.transaction = _noop_txn

        # Fail on the 2nd put_version (the source tombstone) — after the destination is
        # already created and committed.
        original_put = vfs_instance._meta.put_version
        call_count = 0

        async def failing_put(version, *, expected_version=None):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("Simulated tombstone failure")
            return await original_put(version, expected_version=expected_version)

        vfs_instance._meta.put_version = failing_put

        with pytest.raises(RuntimeError, match="Simulated tombstone failure"):
            await vfs_instance.move(ns.id, "/src.py", "/dst.py", principal_id=p.id)

        vfs_instance._meta.put_version = original_put

        # No version lost: the file is readable at BOTH source and destination.
        assert await vfs_instance.read(ns.id, "/src.py", principal_id=p.id) == b"data"
        assert await vfs_instance.read(ns.id, "/dst.py", principal_id=p.id) == b"data"

    @pytest.mark.asyncio
    async def test_move_creates_audit_event(self, vfs_instance):
        ns, p, admin = await _setup_ns_principal(vfs_instance)
        await vfs_instance.write(ns.id, "/a.py", b"data", principal_id=p.id)
        await vfs_instance.move(ns.id, "/a.py", "/b.py", principal_id=p.id)
        rows = await vfs_instance._meta._execute_fetchall(
            "SELECT operation FROM audit_events WHERE namespace_id=?", (ns.id,)
        )
        ops = [r[0] for r in rows]
        assert "move" in ops


# --- Verify-driven additions: CAS, dedup, lazy I/O, ULID format, immutability ---


class TestVFSCopyCAS:
    """MetadataCASSemantics — copy with expected_version exercises the CAS branch."""

    @pytest.mark.asyncio
    async def test_copy_cas_success(self, vfs_instance):
        ns, p, _ = await _setup_ns_principal(vfs_instance)
        # Establish a current version 1 at the destination so we can target it with CAS.
        await vfs_instance.write(ns.id, "/src.py", b"source", principal_id=p.id)
        await vfs_instance.write(ns.id, "/dst.py", b"existing", principal_id=p.id)
        v2 = await vfs_instance.copy(ns.id, "/src.py", "/dst.py", principal_id=p.id, expected_version=1)
        assert v2.version_number == 2

    @pytest.mark.asyncio
    async def test_copy_cas_conflict(self, vfs_instance):
        ns, p, _ = await _setup_ns_principal(vfs_instance)
        await vfs_instance.write(ns.id, "/src.py", b"source", principal_id=p.id)
        await vfs_instance.write(ns.id, "/dst.py", b"existing", principal_id=p.id)
        from vfs.errors import ConflictError

        with pytest.raises(ConflictError):
            await vfs_instance.copy(ns.id, "/src.py", "/dst.py", principal_id=p.id, expected_version=99)


class TestVFSWriteCASSuccess:
    """OptimisticConcurrency — VFS-level happy path complement to existing conflict test."""

    @pytest.mark.asyncio
    async def test_write_cas_success_at_vfs(self, vfs_instance):
        ns, p, _ = await _setup_ns_principal(vfs_instance)
        await vfs_instance.write(ns.id, "/a.py", b"v1", principal_id=p.id)
        v2 = await vfs_instance.write(ns.id, "/a.py", b"v2", principal_id=p.id, expected_version=1)
        assert v2.version_number == 2


class TestVFSDedupAndLazyIO:
    """ContentAddressedStorage (DeduplicatedWrite) + LazyContentResolution."""

    @pytest.mark.asyncio
    async def test_write_dedup_single_blob(self, vfs_instance):
        ns, p, _ = await _setup_ns_principal(vfs_instance)
        await vfs_instance.write(ns.id, "/a.py", b"same-content", principal_id=p.id)
        await vfs_instance.write(ns.id, "/b.py", b"same-content", principal_id=p.id)
        hashes = [h async for h in vfs_instance._blob.list_hashes()]
        assert len(hashes) == 1, f"expected 1 deduped blob, got {len(hashes)}: {hashes}"

    @pytest.mark.asyncio
    async def test_stat_does_not_fetch_blob(self, vfs_instance):
        ns, p, _ = await _setup_ns_principal(vfs_instance)
        await vfs_instance.write(ns.id, "/a.py", b"hello", principal_id=p.id)
        # Spy on blob.get — stat MUST NOT trigger it.
        calls: list[str] = []
        original_get = vfs_instance._blob.get

        async def _spy_get(h):
            calls.append(h)
            return await original_get(h)

        vfs_instance._blob.get = _spy_get
        await vfs_instance.stat(ns.id, "/a.py", principal_id=p.id)
        assert calls == [], f"stat triggered blob fetches: {calls}"

    @pytest.mark.asyncio
    async def test_list_does_not_fetch_blob(self, vfs_instance):
        ns, p, _ = await _setup_ns_principal(vfs_instance)
        for path in ("/a.py", "/b.py", "/c.py"):
            await vfs_instance.write(ns.id, path, b"x", principal_id=p.id)
        calls: list[str] = []
        original_get = vfs_instance._blob.get

        async def _spy_get(h):
            calls.append(h)
            return await original_get(h)

        vfs_instance._blob.get = _spy_get
        await vfs_instance.list(ns.id, "/", principal_id=p.id, recursive=True)
        assert calls == [], f"list triggered blob fetches: {calls}"


class TestULIDIdentifierFormat:
    """ULIDIdentifiers — Namespace.id and VersionMeta.id are 26-character ULID strings."""

    @pytest.mark.asyncio
    async def test_namespace_id_is_ulid_format(self, vfs_instance):
        ns = await vfs_instance.create_namespace("ws-format", "admin")
        assert len(ns.id) == 26, f"expected 26-char ULID, got {len(ns.id)}: {ns.id!r}"
        # ULID base32 charset (Crockford): excludes I, L, O, U
        valid = set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")
        assert set(ns.id).issubset(valid), f"namespace id has non-ULID chars: {ns.id!r}"

    @pytest.mark.asyncio
    async def test_version_id_is_ulid_format(self, vfs_instance):
        ns, p, _ = await _setup_ns_principal(vfs_instance)
        ver = await vfs_instance.write(ns.id, "/a.py", b"x", principal_id=p.id)
        assert len(ver.id) == 26, f"expected 26-char ULID, got {len(ver.id)}: {ver.id!r}"
        valid = set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")
        assert set(ver.id).issubset(valid)


class TestImmutableVersionHistory:
    """ImmutableVersionHistory — prior versions' content fields are not mutated by later writes."""

    @pytest.mark.asyncio
    async def test_write_does_not_mutate_prior_version(self, vfs_instance):
        ns, p, _ = await _setup_ns_principal(vfs_instance)
        v1 = await vfs_instance.write(ns.id, "/a.py", b"v1-content", principal_id=p.id)
        # Snapshot every immutable field.
        snapshot = {
            "id": v1.id,
            "version_number": v1.version_number,
            "content_hash": v1.content_hash,
            "size": v1.size,
            "created_at": v1.created_at,
            "created_by": v1.created_by,
            "is_tombstone": v1.is_tombstone,
            "parent_version_id": v1.parent_version_id,
        }
        # Subsequent writes must not touch v1's record.
        await vfs_instance.write(ns.id, "/a.py", b"v2-content", principal_id=p.id)
        await vfs_instance.write(ns.id, "/a.py", b"v3-content", principal_id=p.id)
        v1_after = await vfs_instance._meta.get_version(ns.id, "/a.py", version_number=1)
        for field, value in snapshot.items():
            assert getattr(v1_after, field) == value, f"v1 {field} mutated"


class TestAbsolutePathsOnly:
    """AbsolutePathsOnly — VFS rejects any non-absolute path argument with ValueError."""

    @pytest.mark.asyncio
    async def test_relative_path_raises_valueerror(self, vfs_instance):
        ns, p, _ = await _setup_ns_principal(vfs_instance)
        # Seed an absolute file so destination/source-style checks can target it.
        await vfs_instance.write(ns.id, "/seed.txt", b"x", principal_id=p.id)

        with pytest.raises(ValueError, match="absolute"):
            await vfs_instance.stat(ns.id, "relative/path", principal_id=p.id)
        with pytest.raises(ValueError, match="absolute"):
            await vfs_instance.list(ns.id, "relative/", principal_id=p.id)
        with pytest.raises(ValueError, match="absolute"):
            await vfs_instance.read(ns.id, "relative/path", principal_id=p.id)
        with pytest.raises(ValueError, match="absolute"):
            await vfs_instance.write(ns.id, "relative/path", b"data", principal_id=p.id)
        with pytest.raises(ValueError, match="absolute"):
            await vfs_instance.delete(ns.id, "relative/path", principal_id=p.id)
        # copy/move: both src and dst must be absolute
        with pytest.raises(ValueError, match="absolute"):
            await vfs_instance.copy(ns.id, "relative/src", "/dst", principal_id=p.id)
        with pytest.raises(ValueError, match="absolute"):
            await vfs_instance.copy(ns.id, "/seed.txt", "relative/dst", principal_id=p.id)
        with pytest.raises(ValueError, match="absolute"):
            await vfs_instance.move(ns.id, "relative/src", "/dst", principal_id=p.id)
        with pytest.raises(ValueError, match="absolute"):
            await vfs_instance.move(ns.id, "/seed.txt", "relative/dst", principal_id=p.id)
        with pytest.raises(ValueError, match="absolute"):
            await vfs_instance.versions(ns.id, "relative/path", principal_id=p.id)
        with pytest.raises(ValueError, match="absolute"):
            await vfs_instance.rollback(ns.id, "relative/path", 1, principal_id=p.id)
        with pytest.raises(ValueError, match="absolute"):
            from vfs.models import SearchType

            await vfs_instance.search(ns.id, "q", "relative/", SearchType.GLOB, principal_id=p.id)
        with pytest.raises(ValueError, match="absolute"):
            await vfs_instance.reindex(ns.id, scope="relative/")

    @pytest.mark.asyncio
    async def test_absolute_path_accepted(self, vfs_instance):
        """Absolute paths must pass the boundary; downstream errors are unrelated."""
        from vfs.errors import NotFoundError

        ns, p, _ = await _setup_ns_principal(vfs_instance)

        # Seed a real file for read/stat/versions/rollback to exercise non-trivial paths.
        await vfs_instance.write(ns.id, "/seed.txt", b"data", principal_id=p.id)

        # write — absolute path must not raise ValueError at boundary
        await vfs_instance.write(ns.id, "/abs.txt", b"x", principal_id=p.id)
        # stat / read on existing file
        await vfs_instance.stat(ns.id, "/seed.txt", principal_id=p.id)
        await vfs_instance.read(ns.id, "/seed.txt", principal_id=p.id)
        # list — absolute prefix accepted
        await vfs_instance.list(ns.id, "/", principal_id=p.id)
        # delete on existing
        await vfs_instance.delete(ns.id, "/abs.txt", principal_id=p.id)
        # copy with both absolute
        await vfs_instance.copy(ns.id, "/seed.txt", "/copy.txt", principal_id=p.id)
        # move with both absolute
        await vfs_instance.move(ns.id, "/copy.txt", "/moved.txt", principal_id=p.id)
        # versions / rollback
        await vfs_instance.versions(ns.id, "/seed.txt", principal_id=p.id)
        await vfs_instance.rollback(ns.id, "/seed.txt", 1, principal_id=p.id)
        # search / reindex — absolute scope accepted
        from vfs.models import SearchType

        await vfs_instance.search(ns.id, "*", "/", SearchType.GLOB, principal_id=p.id)
        await vfs_instance.reindex(ns.id, scope="/")

        # Absolute path to a missing file: NotFoundError, not ValueError
        with pytest.raises(NotFoundError):
            await vfs_instance.stat(ns.id, "/does/not/exist", principal_id=p.id)
