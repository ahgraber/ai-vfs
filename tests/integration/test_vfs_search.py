"""Integration tests for VFS.search (Task 19)."""

from __future__ import annotations

import pytest

from vfs.models import SearchType


async def _setup(vfs):
    ns = await vfs.create_namespace("test-ws", "admin")
    p = await vfs.create_principal("agent")
    await vfs.grant(p.id, ns.id, "/", {"read", "write"})
    return ns, p


class TestVFSSearch:
    @pytest.mark.asyncio
    async def test_glob_search(self, vfs_instance):
        ns, p = await _setup(vfs_instance)
        await vfs_instance.write(ns.id, "/src/a.py", b"x", principal_id=p.id)
        await vfs_instance.write(ns.id, "/src/b.txt", b"x", principal_id=p.id)
        await vfs_instance.write(ns.id, "/src/c.py", b"x", principal_id=p.id)
        results = await vfs_instance.search(ns.id, "*.py", "/src/", SearchType.GLOB, principal_id=p.id)
        paths = {r.path for r in results}
        assert paths == {"/src/a.py", "/src/c.py"}

    @pytest.mark.asyncio
    async def test_find_search(self, vfs_instance):
        ns, p = await _setup(vfs_instance)
        await vfs_instance.write(ns.id, "/a.txt", b"x", principal_id=p.id)
        await vfs_instance.write(ns.id, "/sub/b.txt", b"x", principal_id=p.id)
        await vfs_instance.write(ns.id, "/c.py", b"x", principal_id=p.id)
        results = await vfs_instance.search(ns.id, "*.txt", "/", SearchType.FIND, principal_id=p.id)
        paths = {r.path for r in results}
        assert paths == {"/a.txt", "/sub/b.txt"}

    @pytest.mark.asyncio
    async def test_regex_grep(self, vfs_instance):
        ns, p = await _setup(vfs_instance)
        content = b"line 1\nline 2\nline 3\nline 4\n# TODO: fix\nline 6\n"
        await vfs_instance.write(ns.id, "/src/main.py", content, principal_id=p.id)
        results = await vfs_instance.search(ns.id, "TODO", "/", SearchType.REGEX, principal_id=p.id)
        assert len(results) >= 1
        assert results[0].line_number == 5

    @pytest.mark.asyncio
    async def test_search_scoped_to_permissions(self, vfs_instance):
        ns, p = await _setup(vfs_instance)
        await vfs_instance.write(ns.id, "/public/a.py", b"data", principal_id=p.id)
        await vfs_instance.write(ns.id, "/secret/b.py", b"data", principal_id=p.id)
        limited = await vfs_instance.create_principal("limited")
        await vfs_instance.grant(limited.id, ns.id, "/public/", {"read"})
        results = await vfs_instance.search(ns.id, "*.py", "/", SearchType.FIND, principal_id=limited.id)
        paths = {r.path for r in results}
        assert paths == {"/public/a.py"}

    @pytest.mark.asyncio
    async def test_unknown_capability_rejected(self, vfs_instance):
        """Requesting a search type no provider supports raises ValueError."""
        ns, p = await _setup(vfs_instance)
        with pytest.raises(ValueError, match="semantic"):
            await vfs_instance.search(ns.id, "meaning of life", "/", SearchType.SEMANTIC, principal_id=p.id)
