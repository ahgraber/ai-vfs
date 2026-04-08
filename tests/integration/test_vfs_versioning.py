"""Integration tests for VFS versioning (Task 18)."""

from __future__ import annotations

import pytest


async def _setup(vfs):
    ns = await vfs.create_namespace("test-ws", "admin")
    p = await vfs.create_principal("agent")
    await vfs.grant(p.id, ns.id, "/", {"read", "write", "delete"})
    return ns, p


class TestVFSVersions:
    @pytest.mark.asyncio
    async def test_versions_returns_history(self, vfs_instance):
        ns, p = await _setup(vfs_instance)
        for i in range(3):
            await vfs_instance.write(ns.id, "/a.py", f"v{i + 1}".encode(), principal_id=p.id)
        versions = await vfs_instance.versions(ns.id, "/a.py", principal_id=p.id)
        assert len(versions) == 3
        assert versions[0].version_number == 3  # newest first

    @pytest.mark.asyncio
    async def test_versions_limit_and_before(self, vfs_instance):
        ns, p = await _setup(vfs_instance)
        for i in range(5):
            await vfs_instance.write(ns.id, "/a.py", f"v{i + 1}".encode(), principal_id=p.id)
        versions = await vfs_instance.versions(ns.id, "/a.py", principal_id=p.id, limit=2, before=4)
        assert len(versions) == 2
        assert versions[0].version_number == 3


class TestVFSRollback:
    @pytest.mark.asyncio
    async def test_rollback_creates_new_version(self, vfs_instance):
        ns, p = await _setup(vfs_instance)
        await vfs_instance.write(ns.id, "/a.py", b"v1 content", principal_id=p.id)
        await vfs_instance.write(ns.id, "/a.py", b"v2 content", principal_id=p.id)
        v3 = await vfs_instance.rollback(ns.id, "/a.py", 1, principal_id=p.id)
        assert v3.version_number == 3

    @pytest.mark.asyncio
    async def test_rollback_is_new_version_not_mutation(self, vfs_instance):
        ns, p = await _setup(vfs_instance)
        await vfs_instance.write(ns.id, "/a.py", b"v1", principal_id=p.id)
        await vfs_instance.write(ns.id, "/a.py", b"v2", principal_id=p.id)
        await vfs_instance.rollback(ns.id, "/a.py", 1, principal_id=p.id)
        versions = await vfs_instance.versions(ns.id, "/a.py", principal_id=p.id)
        nums = {v.version_number for v in versions}
        assert nums == {1, 2, 3}  # all three exist; rollback created v3, did not mutate v1 or v2

    @pytest.mark.asyncio
    async def test_rollback_read_returns_v1_content(self, vfs_instance):
        ns, p = await _setup(vfs_instance)
        await vfs_instance.write(ns.id, "/a.py", b"original", principal_id=p.id)
        await vfs_instance.write(ns.id, "/a.py", b"modified", principal_id=p.id)
        await vfs_instance.rollback(ns.id, "/a.py", 1, principal_id=p.id)
        content = await vfs_instance.read(ns.id, "/a.py", principal_id=p.id)
        assert content == b"original"

    @pytest.mark.asyncio
    async def test_rollback_creates_audit_event(self, vfs_instance):
        ns, p = await _setup(vfs_instance)
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
        ns, p = await _setup(vfs_instance)
        await vfs_instance.write(ns.id, "/a.py", b"v1", principal_id=p.id)
        delete_only = await vfs_instance.create_principal("deleter")
        await vfs_instance.grant(delete_only.id, ns.id, "/", {"delete"})
        from vfs.errors import PermissionDeniedError

        with pytest.raises(PermissionDeniedError):
            await vfs_instance.rollback(ns.id, "/a.py", 1, principal_id=delete_only.id)
