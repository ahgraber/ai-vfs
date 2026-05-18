"""Integration tests for access control and public API (Task 22 + verify-driven additions)."""

from __future__ import annotations

import pytest

from vfs.errors import PermissionDeniedError


class TestAccessControl:
    @pytest.mark.asyncio
    async def test_grant_and_use_permission(self, vfs_instance, admin_factory):
        ns = await vfs_instance.create_namespace("ws", "admin")
        admin = await admin_factory(ns.id)
        bob = await vfs_instance.create_principal("bob")
        await vfs_instance.grant(admin.id, bob.id, ns.id, "/", {"read", "write"})
        await vfs_instance.write(ns.id, "/file.py", b"hello", principal_id=bob.id)
        content = await vfs_instance.read(ns.id, "/file.py", principal_id=bob.id)
        assert content == b"hello"

    @pytest.mark.asyncio
    async def test_cross_namespace_denied(self, vfs_instance, admin_factory):
        ns_a = await vfs_instance.create_namespace("ws-a", "admin")
        ns_b = await vfs_instance.create_namespace("ws-b", "admin")
        admin_a = await admin_factory(ns_a.id, "admin-a")
        p = await vfs_instance.create_principal("agent")
        await vfs_instance.grant(admin_a.id, p.id, ns_a.id, "/", {"read", "write"})
        await vfs_instance.write(ns_a.id, "/file.py", b"data", principal_id=p.id)
        with pytest.raises(PermissionDeniedError):
            await vfs_instance.read(ns_b.id, "/file.py", principal_id=p.id)

    @pytest.mark.asyncio
    async def test_admin_grants_subtree(self, vfs_instance, admin_factory):
        ns = await vfs_instance.create_namespace("ws", "admin")
        admin = await admin_factory(ns.id)
        bob = await vfs_instance.create_principal("bob")
        await vfs_instance.grant(admin.id, bob.id, ns.id, "/workspace/docs/", {"write"})
        await vfs_instance.grant(admin.id, bob.id, ns.id, "/workspace/", {"read"})
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
    async def test_execute_permission_storable(self, vfs_instance, admin_factory):
        ns = await vfs_instance.create_namespace("ws", "admin")
        admin = await admin_factory(ns.id)
        p = await vfs_instance.create_principal("agent")
        await vfs_instance.grant(admin.id, p.id, ns.id, "/workspace/", {"execute"})
        has_exec = await vfs_instance._meta.check_permission(p.id, ns.id, "/workspace/script.sh", "execute")
        assert has_exec is True

    @pytest.mark.asyncio
    async def test_admin_permission_storable(self, vfs_instance, admin_factory):
        """OperationGranularity: admin operation can be stored and used to gate further grants."""
        ns = await vfs_instance.create_namespace("ws", "admin")
        admin = await admin_factory(ns.id)
        delegate = await vfs_instance.create_principal("delegate")
        # admin grants admin on /workspace/ to delegate
        await vfs_instance.grant(admin.id, delegate.id, ns.id, "/workspace/", {"admin"})
        # The delegate can now grant within /workspace/
        recipient = await vfs_instance.create_principal("recipient")
        await vfs_instance.grant(delegate.id, recipient.id, ns.id, "/workspace/", {"read"})
        has_read = await vfs_instance._meta.check_permission(recipient.id, ns.id, "/workspace/file.txt", "read")
        assert has_read is True

    # --- New scenarios from PermissionGranting spec edit ---

    @pytest.mark.asyncio
    async def test_non_admin_cannot_grant(self, vfs_instance, admin_factory):
        """PermissionGranting: principal lacking admin on the target subtree cannot grant."""
        ns = await vfs_instance.create_namespace("ws", "admin")
        admin = await admin_factory(ns.id)
        non_admin = await vfs_instance.create_principal("non-admin")
        recipient = await vfs_instance.create_principal("recipient")
        # non_admin has read+write but no admin
        await vfs_instance.grant(admin.id, non_admin.id, ns.id, "/workspace/", {"read", "write"})
        with pytest.raises(PermissionDeniedError):
            await vfs_instance.grant(non_admin.id, recipient.id, ns.id, "/workspace/", {"read"})
        # recipient gained no permissions
        has_read = await vfs_instance._meta.check_permission(recipient.id, ns.id, "/workspace/file.txt", "read")
        assert has_read is False

    @pytest.mark.asyncio
    async def test_bootstrap_admin_creates_first_admin(self, vfs_instance):
        """PermissionGranting: bootstrap_admin creates the initial admin on an empty namespace."""
        ns = await vfs_instance.create_namespace("ws", "admin")
        first = await vfs_instance.create_principal("first-admin")
        await vfs_instance.bootstrap_admin(first.id, ns.id)
        # first now holds admin on /
        has_admin = await vfs_instance._meta.check_permission(first.id, ns.id, "/", "admin")
        assert has_admin is True
        # first can grant via the normal admin-gated path
        second = await vfs_instance.create_principal("second")
        await vfs_instance.grant(first.id, second.id, ns.id, "/", {"read"})
        has_read = await vfs_instance._meta.check_permission(second.id, ns.id, "/file", "read")
        assert has_read is True

    @pytest.mark.asyncio
    async def test_bootstrap_admin_rejected_when_admin_exists(self, vfs_instance, admin_factory):
        """PermissionGranting: bootstrap_admin is single-use per namespace."""
        ns = await vfs_instance.create_namespace("ws", "admin")
        # admin_factory bootstraps the first admin
        await admin_factory(ns.id)
        # Second bootstrap_admin call must fail
        second = await vfs_instance.create_principal("second-admin")
        with pytest.raises(PermissionDeniedError):
            await vfs_instance.bootstrap_admin(second.id, ns.id)

    @pytest.mark.asyncio
    async def test_grant_creates_audit_event(self, vfs_instance, admin_factory):
        """AuditLogStateChanges: grant() emits an audit event with operation='permission_change'."""
        ns = await vfs_instance.create_namespace("ws", "admin")
        admin = await admin_factory(ns.id)
        recipient = await vfs_instance.create_principal("recipient")
        await vfs_instance.grant(admin.id, recipient.id, ns.id, "/data/", {"read"})
        rows = await vfs_instance._meta._execute_fetchall(
            "SELECT operation, principal_id, detail FROM audit_events WHERE namespace_id=? AND operation=?",
            (ns.id, "permission_change"),
        )
        assert len(rows) == 1
        assert rows[0][1] == admin.id  # granter is the principal_id on the audit event
        import json

        detail = json.loads(rows[0][2])
        assert detail["target_principal_id"] == recipient.id
        assert detail["path_prefix"] == "/data/"
        assert detail["operations"] == ["read"]

    @pytest.mark.asyncio
    async def test_bootstrap_admin_creates_audit_event(self, vfs_instance):
        """bootstrap_admin emits an audit event with operation='bootstrap_admin'."""
        ns = await vfs_instance.create_namespace("ws", "admin")
        admin = await vfs_instance.create_principal("first-admin")
        await vfs_instance.bootstrap_admin(admin.id, ns.id)
        rows = await vfs_instance._meta._execute_fetchall(
            "SELECT operation, principal_id FROM audit_events WHERE namespace_id=? AND operation=?",
            (ns.id, "bootstrap_admin"),
        )
        assert len(rows) == 1
        assert rows[0][1] == admin.id
