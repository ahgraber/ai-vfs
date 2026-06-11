"""Integration tests for VFS versioning (Task 18)."""

from __future__ import annotations

import pytest


async def _setup(vfs):
    """Bootstrap an admin + agent. Returns (namespace, agent_principal, admin_principal)."""
    ns = await vfs.create_namespace("test-ws", "admin")
    admin = await vfs.create_principal("test-admin")
    await vfs.bootstrap_admin(admin.id, ns.id)
    p = await vfs.create_principal("agent")
    await vfs.grant(admin.id, p.id, ns.id, "/", {"read", "write", "delete"})
    return ns, p, admin


class TestVFSVersions:
    @pytest.mark.asyncio
    async def test_versions_returns_history(self, vfs_instance):
        ns, p, admin = await _setup(vfs_instance)
        for i in range(3):
            await vfs_instance.write(ns.id, "/a.py", f"v{i + 1}".encode(), principal_id=p.id)
        versions = await vfs_instance.versions(ns.id, "/a.py", principal_id=p.id)
        assert len(versions) == 3
        assert versions[0].version_number == 3  # newest first

    @pytest.mark.asyncio
    async def test_versions_limit_and_before(self, vfs_instance):
        ns, p, admin = await _setup(vfs_instance)
        for i in range(5):
            await vfs_instance.write(ns.id, "/a.py", f"v{i + 1}".encode(), principal_id=p.id)
        versions = await vfs_instance.versions(ns.id, "/a.py", principal_id=p.id, limit=2, before=4)
        assert len(versions) == 2
        assert versions[0].version_number == 3


class TestVFSRollback:
    @pytest.mark.asyncio
    async def test_rollback_creates_new_version(self, vfs_instance):
        ns, p, admin = await _setup(vfs_instance)
        await vfs_instance.write(ns.id, "/a.py", b"v1 content", principal_id=p.id)
        await vfs_instance.write(ns.id, "/a.py", b"v2 content", principal_id=p.id)
        v3 = await vfs_instance.rollback(ns.id, "/a.py", 1, principal_id=p.id)
        assert v3.version_number == 3

    @pytest.mark.asyncio
    async def test_rollback_is_new_version_not_mutation(self, vfs_instance):
        ns, p, admin = await _setup(vfs_instance)
        await vfs_instance.write(ns.id, "/a.py", b"v1", principal_id=p.id)
        await vfs_instance.write(ns.id, "/a.py", b"v2", principal_id=p.id)
        await vfs_instance.rollback(ns.id, "/a.py", 1, principal_id=p.id)
        versions = await vfs_instance.versions(ns.id, "/a.py", principal_id=p.id)
        nums = {v.version_number for v in versions}
        assert nums == {1, 2, 3}  # all three exist; rollback created v3, did not mutate v1 or v2

    @pytest.mark.asyncio
    async def test_rollback_read_returns_v1_content(self, vfs_instance):
        ns, p, admin = await _setup(vfs_instance)
        await vfs_instance.write(ns.id, "/a.py", b"original", principal_id=p.id)
        await vfs_instance.write(ns.id, "/a.py", b"modified", principal_id=p.id)
        await vfs_instance.rollback(ns.id, "/a.py", 1, principal_id=p.id)
        content = await vfs_instance.read(ns.id, "/a.py", principal_id=p.id)
        assert content == b"original"

    @pytest.mark.asyncio
    async def test_rollback_creates_audit_event(self, vfs_instance):
        ns, p, admin = await _setup(vfs_instance)
        await vfs_instance.write(ns.id, "/a.py", b"v1", principal_id=p.id)
        await vfs_instance.write(ns.id, "/a.py", b"v2", principal_id=p.id)
        await vfs_instance.rollback(ns.id, "/a.py", 1, principal_id=p.id)
        rows = await vfs_instance._meta._execute_fetchall(
            "SELECT operation FROM audit_events WHERE namespace_id=?", (ns.id,)
        )
        ops = [r[0] for r in rows]
        assert "rollback" in ops

    @pytest.mark.asyncio
    async def test_rollback_permission_denied(self, vfs_instance):
        ns, p, admin = await _setup(vfs_instance)
        await vfs_instance.write(ns.id, "/a.py", b"v1", principal_id=p.id)
        delete_only = await vfs_instance.create_principal("deleter")
        await vfs_instance.grant(admin.id, delete_only.id, ns.id, "/", {"delete"})
        from vfs.errors import PermissionDeniedError

        with pytest.raises(PermissionDeniedError):
            await vfs_instance.rollback(ns.id, "/a.py", 1, principal_id=delete_only.id)

    @pytest.mark.asyncio
    async def test_rollback_sets_parent_version_id(self, vfs_instance):
        """RollbackCreatesNewVersion: the new version's parent_version_id points at the target."""
        ns, p, admin = await _setup(vfs_instance)
        v1 = await vfs_instance.write(ns.id, "/a.py", b"v1", principal_id=p.id)
        await vfs_instance.write(ns.id, "/a.py", b"v2", principal_id=p.id)
        v3 = await vfs_instance.rollback(ns.id, "/a.py", 1, principal_id=p.id)
        assert v3.parent_version_id == v1.id


class TestVFSVersionsPermissions:
    @pytest.mark.asyncio
    async def test_versions_permission_denied(self, vfs_instance):
        """DefaultDeny: principal with no permissions cannot list versions."""
        ns, p, admin = await _setup(vfs_instance)
        await vfs_instance.write(ns.id, "/a.py", b"v1", principal_id=p.id)
        no_perm = await vfs_instance.create_principal("no-perm")
        from vfs.errors import PermissionDeniedError

        with pytest.raises(PermissionDeniedError):
            await vfs_instance.versions(ns.id, "/a.py", principal_id=no_perm.id)


class TestVFSReindex:
    @pytest.mark.asyncio
    async def test_reindex_backfills_search_meta(self, vfs_instance):
        """SearchMetaReindex: reindex() populates search_meta via the active provider.

        # phase2-search (archived change): when the metadata store exposes NativeTextSearch
        # (SQLite FTS5 here), reindex() calls nts.index_text() directly and never calls
        # self._search.index() — so search_meta carries the NTS key, not the stub's key.
        # Assertions are subset checks (NTS key present and ready) rather than exact equality.
        """
        from vfs.models import SearchArtifact, SearchType

        ns, p, admin = await _setup(vfs_instance)
        # Write files BEFORE swapping the provider, so default provider records {} on disk.
        await vfs_instance.write(ns.id, "/a.py", b"alpha", principal_id=p.id)
        await vfs_instance.write(ns.id, "/b.py", b"beta", principal_id=p.id)

        class _StubProvider:
            async def index(self, path, content, meta):
                # reindex() with NTS active never calls this; return None per protocol
                return None

            async def search(self, query, scope, search_type, candidates, fetch_content=None):
                return []

            def capabilities(self):
                return {SearchType.GLOB}

        vfs_instance._search = _StubProvider()
        updated = await vfs_instance.reindex(ns.id)
        assert updated == 2

        nts = vfs_instance._meta.native_text_search()
        nts_key = nts.provider_key  # "vfs.sqlite_fts5"

        ver_a = await vfs_instance._meta.get_version(ns.id, "/a.py")
        ver_b = await vfs_instance._meta.get_version(ns.id, "/b.py")
        # NTS key must be present and ready; custom stub key absent because reindex()
        # with NTS active routes through nts.index_text(), not self._search.index().
        assert nts_key in ver_a.search_meta
        assert SearchArtifact.from_dict(ver_a.search_meta[nts_key]).status == "ready"
        assert nts_key in ver_b.search_meta
        assert SearchArtifact.from_dict(ver_b.search_meta[nts_key]).status == "ready"
