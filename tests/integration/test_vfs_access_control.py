"""Integration tests for access control and public API (Task 22)."""

from __future__ import annotations

import pytest

from vfs.errors import PermissionDeniedError


class TestAccessControl:
    @pytest.mark.asyncio
    async def test_grant_and_use_permission(self, vfs_instance):
        ns = await vfs_instance.create_namespace("ws", "admin")
        admin = await vfs_instance.create_principal("admin")
        bob = await vfs_instance.create_principal("bob")
        await vfs_instance.grant(admin.id, ns.id, "/", {"read", "write"})
        await vfs_instance.grant(bob.id, ns.id, "/", {"read", "write"})
        await vfs_instance.write(ns.id, "/file.py", b"hello", principal_id=bob.id)
        content = await vfs_instance.read(ns.id, "/file.py", principal_id=bob.id)
        assert content == b"hello"

    @pytest.mark.asyncio
    async def test_cross_namespace_denied(self, vfs_instance):
        ns_a = await vfs_instance.create_namespace("ws-a", "admin")
        ns_b = await vfs_instance.create_namespace("ws-b", "admin")
        p = await vfs_instance.create_principal("agent")
        await vfs_instance.grant(p.id, ns_a.id, "/", {"read", "write"})
        await vfs_instance.write(ns_a.id, "/file.py", b"data", principal_id=p.id)
        with pytest.raises(PermissionDeniedError):
            await vfs_instance.read(ns_b.id, "/file.py", principal_id=p.id)

    @pytest.mark.asyncio
    async def test_admin_grants_subtree(self, vfs_instance):
        ns = await vfs_instance.create_namespace("ws", "admin")
        admin = await vfs_instance.create_principal("admin")
        bob = await vfs_instance.create_principal("bob")
        await vfs_instance.grant(admin.id, ns.id, "/", {"read", "write"})
        await vfs_instance.grant(bob.id, ns.id, "/workspace/docs/", {"write"})
        await vfs_instance.grant(bob.id, ns.id, "/workspace/", {"read"})
        # Write within docs
        await vfs_instance.write(
            ns.id,
            "/workspace/docs/readme.md",
            b"hello",
            principal_id=bob.id,
        )
        # Cannot write outside docs
        with pytest.raises(PermissionDeniedError):
            await vfs_instance.write(
                ns.id,
                "/config.yaml",
                b"bad",
                principal_id=bob.id,
            )

    @pytest.mark.asyncio
    async def test_name_resolution_namespace_ulid(self, vfs_instance):
        ns = await vfs_instance.create_namespace("my-workspace", "admin")
        resolved = await vfs_instance.resolve_name("namespace", "my-workspace")
        assert resolved == ns.id

    @pytest.mark.asyncio
    async def test_name_resolution_principal_uuid4(self, vfs_instance):
        import uuid

        p = await vfs_instance.create_principal("agent-bob")
        resolved = await vfs_instance.resolve_name("principal", "agent-bob")
        assert resolved == p.id
        # Verify it's a valid UUID4
        parsed = uuid.UUID(p.id)
        assert parsed.version == 4

    @pytest.mark.asyncio
    async def test_execute_permission_storable(self, vfs_instance):
        ns = await vfs_instance.create_namespace("ws", "admin")
        p = await vfs_instance.create_principal("agent")
        await vfs_instance.grant(p.id, ns.id, "/workspace/", {"execute"})
        has_exec = await vfs_instance._meta.check_permission(p.id, ns.id, "/workspace/script.sh", "execute")
        assert has_exec is True
