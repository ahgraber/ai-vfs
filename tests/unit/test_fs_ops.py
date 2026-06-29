"""Unit tests for FsOperations and fs_operations_for factory.

Covers:
  FsOperationsFactory/RelativePathResolved
  FsOperationsRateLimiting/BudgetExceededOnOverflow
  FsOperationsRateLimiting/CounterFreshPerExecution
  ShellOperationsLayer/GrepDispatchesToSearch
  ShellOperationsLayer/GrepPropagatesColdIndex
  ShellOperationsLayer/FindWithPredicates
  ShellOperationsLayer/GlobPatternMatch
  ShellOperationsLayer/LsStructuredOutput
  ShellOperationsLayer/LsLongIncludesSize
  ShellOperationsLayer/LsSynthesizesDirectories  [reviewer finding 4]
  ShellOperationsLayer/OversizedReadReturnsError
  ShellOperationsLayer/OversizedStatBeforeRead   [reviewer finding 5]
  ShellOperationsLayer/BinaryFileReturnsError
  ShellOperationsLayer/HeadTailSlice
  ShellOperationsLayer/EditDelegatesToAnchoredEditing
  ShellOperationsLayer/WriteReturnsMarshalableDict  [reviewer finding 1]
  ShellOperationsLayer/GrepRecursiveParam           [reviewer finding 7]
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from vfs.anchored_editing import resolve_anchor
from vfs.config import VFSConfig
from vfs.errors import AnchorConflictError, OperationBudgetExceededError, ReindexRequiredError
from vfs.execution.fs_ops import FsOperations, OperationCounter, fs_operations_for
from vfs.protocols.execution import ResourceLimits
from vfs.session import Session
from vfs.vfs import VFS

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _setup_vfs(vfs: VFS):
    """Bootstrap a namespace and a principal with full access; return (ns, principal)."""
    ns = await vfs.create_namespace("testns", "admin")
    admin = await vfs.create_principal("admin-user")
    await vfs.bootstrap_admin(admin.id, ns.id)
    agent = await vfs.create_principal("agent-user")
    await vfs.grant(admin.id, agent.id, ns.id, "/", {"read", "write", "delete", "admin"})
    return ns, agent


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def vfs_inst(tmp_path):
    """Lightweight VFS backed by SQLite + local FS blob."""
    db_path = str(tmp_path / "test.db")
    blob_path = str(tmp_path / "blobs")
    config = VFSConfig(
        metadata_store_uri=f"sqlite:///{db_path}",
        blob_store_uri=f"file:///{blob_path}/",
        otel_enabled=False,
        audit_log_enabled=False,
        blob_cache_enabled=False,
    )
    v = VFS(config)
    await v.initialize()
    yield v
    await v.close()


@pytest_asyncio.fixture
async def env(vfs_inst):
    """Set up namespace + principal; return (vfs, session, ns, agent)."""
    ns, agent = await _setup_vfs(vfs_inst)
    session = Session(vfs_inst, ns.id, agent.id)
    return vfs_inst, session, ns, agent


# ---------------------------------------------------------------------------
# OperationCounter direct tests
# ---------------------------------------------------------------------------


class TestOperationCounter:
    """OperationCounter increments and raises at the limit."""

    def test_increments(self):
        c = OperationCounter(3)
        c.check_and_increment()
        c.check_and_increment()
        assert c.count == 2

    def test_raises_at_limit(self):
        c = OperationCounter(2)
        c.check_and_increment()
        c.check_and_increment()
        with pytest.raises(OperationBudgetExceededError):
            c.check_and_increment()

    def test_raises_before_operation(self):
        """BudgetExceededOnOverflow: error raised BEFORE underlying op (counter at limit)."""
        c = OperationCounter(0)
        with pytest.raises(OperationBudgetExceededError):
            c.check_and_increment()


# ---------------------------------------------------------------------------
# FsOperationsFactory/RelativePathResolved
# ---------------------------------------------------------------------------


class TestRelativePathResolved:
    """FsOperationsFactory/RelativePathResolved: relative paths resolved via session.cwd."""

    @pytest.mark.asyncio
    async def test_cat_relative_path(self, env):
        """cat("utils.py") on session with cwd="/src/" calls read("/src/utils.py")."""
        vfs_obj, session, ns, agent = env

        # Write a file at the absolute path we expect to be resolved.
        content = b"def hello(): pass\n"
        await vfs_obj.write(ns.id, "/src/utils.py", content, principal_id=agent.id)

        # Change session cwd to /src/
        await session.cd("/src/")
        assert session.pwd() == "/src/"

        limits = ResourceLimits()
        fs_ops = fs_operations_for(session, limits)

        # cat("utils.py") should resolve to /src/utils.py
        result = await fs_ops.cat("utils.py")
        assert result["error"] is None
        assert "def hello(): pass" in result["lines"]


# ---------------------------------------------------------------------------
# FsOperationsRateLimiting/BudgetExceededOnOverflow
# ---------------------------------------------------------------------------


class TestBudgetExceededOnOverflow:
    """FsOperationsRateLimiting/BudgetExceededOnOverflow."""

    @pytest.mark.asyncio
    async def test_1001st_call_raises(self, env):
        """The 1001st shell wrapper call raises OperationBudgetExceededError."""
        vfs_obj, session, ns, agent = env
        await vfs_obj.write(ns.id, "/file.txt", b"hi", principal_id=agent.id)

        limits = ResourceLimits(max_operations=1000)
        fs_ops = fs_operations_for(session, limits)

        # Exhaust 1000 operations using pwd (cheapest wrapper, no real I/O beyond counter).
        for _ in range(1000):
            await fs_ops.pwd()

        # The 1001st must raise before any VFS call.
        with pytest.raises(OperationBudgetExceededError):
            await fs_ops.pwd()

    @pytest.mark.asyncio
    async def test_budget_enforced_before_underlying_op(self, env):
        """Counter raises BEFORE the VFS operation so no side-effects occur."""
        vfs_obj, session, ns, agent = env

        limits = ResourceLimits(max_operations=0)
        fs_ops = fs_operations_for(session, limits)

        # Even the very first call should be rejected.
        with pytest.raises(OperationBudgetExceededError):
            await fs_ops.pwd()


# ---------------------------------------------------------------------------
# FsOperationsRateLimiting/CounterFreshPerExecution
# ---------------------------------------------------------------------------


class TestCounterFreshPerExecution:
    """FsOperationsRateLimiting/CounterFreshPerExecution."""

    @pytest.mark.asyncio
    async def test_separate_factories_have_independent_counters(self, env):
        """Two fs_operations_for calls produce independent counters."""
        _vfs, session, _ns, _agent = env
        limits = ResourceLimits(max_operations=2)

        fs_ops_a = fs_operations_for(session, limits)
        fs_ops_b = fs_operations_for(session, limits)

        # Exhaust A
        await fs_ops_a.pwd()
        await fs_ops_a.pwd()
        with pytest.raises(OperationBudgetExceededError):
            await fs_ops_a.pwd()

        # B must still have a full budget.
        await fs_ops_b.pwd()
        await fs_ops_b.pwd()
        with pytest.raises(OperationBudgetExceededError):
            await fs_ops_b.pwd()


# ---------------------------------------------------------------------------
# ShellOperationsLayer/GrepDispatchesToSearch
# ---------------------------------------------------------------------------


class TestGrepDispatchesToSearch:
    """ShellOperationsLayer/GrepDispatchesToSearch."""

    @pytest.mark.asyncio
    async def test_grep_returns_matching_results(self, env):
        """grep(pattern, path) invokes REGEX search and returns matching hits."""
        vfs_obj, session, ns, agent = env

        # Write files: one that matches, one that doesn't.
        await vfs_obj.write(ns.id, "/src/match.py", b"def hello(): pass\n", principal_id=agent.id)
        await vfs_obj.write(ns.id, "/src/nomatch.py", b"x = 1\n", principal_id=agent.id)

        limits = ResourceLimits()
        fs_ops = fs_operations_for(session, limits)

        result = await fs_ops.grep("hello", "/src/")
        paths = [r["path"] for r in result["results"]]
        assert "/src/match.py" in paths
        assert "/src/nomatch.py" not in paths


# ---------------------------------------------------------------------------
# ShellOperationsLayer/GrepPropagatesColdIndex
# ---------------------------------------------------------------------------


class TestGrepPropagatesColdIndex:
    """ShellOperationsLayer/GrepPropagatesColdIndex."""

    @pytest.mark.asyncio
    async def test_grep_propagates_reindex_required(self, env):
        """grep re-raises ReindexRequiredError unchanged when search index is cold."""
        vfs_obj, session, ns, agent = env

        # Write more files than the default max_content_reads=10 to trigger cold-index.
        for i in range(12):
            await vfs_obj.write(ns.id, f"/src/f{i}.py", b"content", principal_id=agent.id)

        limits = ResourceLimits()
        fs_ops = fs_operations_for(session, limits)

        # The default VFS uses SQLite with FTS5 if available, or brute-force otherwise.
        # To reliably test cold-index, patch session._vfs.search to raise.
        original_search = vfs_obj.search

        async def _cold_search(*args, **kwargs):
            raise ReindexRequiredError("index cold")

        vfs_obj.search = _cold_search
        try:
            with pytest.raises(ReindexRequiredError):
                await fs_ops.grep("pattern", "/src/")
        finally:
            vfs_obj.search = original_search


# ---------------------------------------------------------------------------
# ShellOperationsLayer/FindWithPredicates
# ---------------------------------------------------------------------------


class TestFindWithPredicates:
    """ShellOperationsLayer/FindWithPredicates."""

    @pytest.mark.asyncio
    async def test_find_name_and_size_predicates(self, env):
        """find(path, name='*.py', size_max=10000) returns only matching files."""
        vfs_obj, session, ns, agent = env

        await vfs_obj.write(ns.id, "/code/app.py", b"pass\n", principal_id=agent.id)
        await vfs_obj.write(ns.id, "/code/data.txt", b"text content\n", principal_id=agent.id)
        await vfs_obj.write(ns.id, "/code/big.py", b"x" * 20000, principal_id=agent.id)

        limits = ResourceLimits()
        fs_ops = fs_operations_for(session, limits)

        result = await fs_ops.find("/code/", name="*.py", size_max=10000)
        paths = [r["path"] for r in result["results"]]
        assert "/code/app.py" in paths
        assert "/code/data.txt" not in paths
        assert "/code/big.py" not in paths


# ---------------------------------------------------------------------------
# ShellOperationsLayer/GlobPatternMatch
# ---------------------------------------------------------------------------


class TestGlobPatternMatch:
    """ShellOperationsLayer/GlobPatternMatch."""

    @pytest.mark.asyncio
    async def test_glob_returns_only_matching_extension(self, env):
        """glob('*.py') returns only .py files in cwd."""
        vfs_obj, session, ns, agent = env

        await vfs_obj.write(ns.id, "/proj/a.py", b"pass", principal_id=agent.id)
        await vfs_obj.write(ns.id, "/proj/b.py", b"pass", principal_id=agent.id)
        await vfs_obj.write(ns.id, "/proj/c.txt", b"text", principal_id=agent.id)

        await session.cd("/proj/")

        limits = ResourceLimits()
        fs_ops = fs_operations_for(session, limits)

        result = await fs_ops.glob("*.py")
        paths = [r["path"] for r in result["results"]]
        assert "/proj/a.py" in paths
        assert "/proj/b.py" in paths
        assert "/proj/c.txt" not in paths


# ---------------------------------------------------------------------------
# ShellOperationsLayer/LsStructuredOutput
# ---------------------------------------------------------------------------


class TestLsStructuredOutput:
    """ShellOperationsLayer/LsStructuredOutput."""

    @pytest.mark.asyncio
    async def test_ls_returns_structured_dicts(self, env):
        """ls(path) returns dicts with name, path, is_dir, version_number, updated_at; no size."""
        vfs_obj, session, ns, agent = env

        await vfs_obj.write(ns.id, "/workspace/foo.py", b"pass", principal_id=agent.id)

        limits = ResourceLimits()
        fs_ops = fs_operations_for(session, limits)

        result = await fs_ops.ls("/workspace/")
        assert not result["truncated"]
        entries = result["entries"]
        assert len(entries) >= 1

        entry = next(e for e in entries if e["path"] == "/workspace/foo.py")
        assert entry["name"] == "foo.py"
        assert entry["path"] == "/workspace/foo.py"
        assert entry["is_dir"] is False
        assert isinstance(entry["version_number"], int)
        assert isinstance(entry["updated_at"], datetime)
        assert "size" not in entry


# ---------------------------------------------------------------------------
# ShellOperationsLayer/LsLongIncludesSize
# ---------------------------------------------------------------------------


class TestLsLongIncludesSize:
    """ShellOperationsLayer/LsLongIncludesSize."""

    @pytest.mark.asyncio
    async def test_ls_long_includes_size(self, env):
        """ls(path, long=True) includes size from VersionMeta lookup."""
        vfs_obj, session, ns, agent = env

        content = b"hello world"
        await vfs_obj.write(ns.id, "/workspace/hello.txt", content, principal_id=agent.id)

        limits = ResourceLimits()
        fs_ops = fs_operations_for(session, limits)

        result = await fs_ops.ls("/workspace/", long=True)
        entries = result["entries"]

        entry = next(e for e in entries if e["path"] == "/workspace/hello.txt")
        assert "size" in entry
        assert entry["size"] == len(content)


# ---------------------------------------------------------------------------
# ShellOperationsLayer/OversizedReadReturnsError
# ---------------------------------------------------------------------------


class TestOversizedReadReturnsError:
    """ShellOperationsLayer/OversizedReadReturnsError."""

    @pytest.mark.asyncio
    async def test_cat_oversized_returns_structured_error(self, env):
        """cat on a file exceeding max_read_bytes returns error dict; no anchors."""
        vfs_obj, session, ns, agent = env

        content = b"a" * 1000
        await vfs_obj.write(ns.id, "/big.txt", content, principal_id=agent.id)

        limits = ResourceLimits(max_read_bytes=100)
        fs_ops = fs_operations_for(session, limits)

        result = await fs_ops.cat("/big.txt")
        assert result["error"] is not None
        assert result["error"]["code"] == "oversized_read"
        assert result["lines"] == []
        assert result["anchors"] == {}


# ---------------------------------------------------------------------------
# ShellOperationsLayer/BinaryFileReturnsError
# ---------------------------------------------------------------------------


class TestBinaryFileReturnsError:
    """ShellOperationsLayer/BinaryFileReturnsError."""

    @pytest.mark.asyncio
    async def test_cat_binary_returns_structured_error(self, env):
        """cat on non-UTF-8 content returns error dict; no anchors."""
        vfs_obj, session, ns, agent = env

        binary_content = bytes(range(256))  # guaranteed non-UTF-8
        await vfs_obj.write(ns.id, "/binary.bin", binary_content, principal_id=agent.id)

        limits = ResourceLimits()
        fs_ops = fs_operations_for(session, limits)

        result = await fs_ops.cat("/binary.bin")
        assert result["error"] is not None
        assert result["error"]["code"] == "binary_content"
        assert result["lines"] == []
        assert result["anchors"] == {}


# ---------------------------------------------------------------------------
# ShellOperationsLayer/HeadTailSlice
# ---------------------------------------------------------------------------


class TestHeadTailSlice:
    """ShellOperationsLayer/HeadTailSlice."""

    @pytest.mark.asyncio
    async def test_head_returns_first_n_lines(self, env):
        """head(path, 5) returns the first 5 lines."""
        vfs_obj, session, ns, agent = env

        lines = [f"line {i}" for i in range(20)]
        content = "\n".join(lines).encode()
        await vfs_obj.write(ns.id, "/lines.txt", content, principal_id=agent.id)

        limits = ResourceLimits()
        fs_ops = fs_operations_for(session, limits)

        result = await fs_ops.head("/lines.txt", 5)
        assert result["error"] is None
        assert result["lines"] == lines[:5]

    @pytest.mark.asyncio
    async def test_tail_returns_last_n_lines(self, env):
        """tail(path, 5) returns the last 5 lines."""
        vfs_obj, session, ns, agent = env

        lines = [f"line {i}" for i in range(20)]
        content = "\n".join(lines).encode()
        await vfs_obj.write(ns.id, "/lines.txt", content, principal_id=agent.id)

        limits = ResourceLimits()
        fs_ops = fs_operations_for(session, limits)

        result = await fs_ops.tail("/lines.txt", 5)
        assert result["error"] is None
        assert result["lines"] == lines[-5:]

    @pytest.mark.asyncio
    async def test_head_anchors_sliced_lines_only(self, env):
        """head returns content-derived anchors for exactly the returned lines."""
        vfs_obj, session, ns, agent = env

        lines = [f"line {i}" for i in range(10)]
        content = "\n".join(lines).encode()
        await vfs_obj.write(ns.id, "/lines.txt", content, principal_id=agent.id)

        fs_ops = fs_operations_for(session, ResourceLimits())

        result = await fs_ops.head("/lines.txt", 3)
        assert result["error"] is None
        assert result["lines"] == lines[:3]
        # Anchors for exactly the 3 returned lines, keyed by absolute index, resolvable.
        assert set(result["anchors"]) == {0, 1, 2}
        assert resolve_anchor(result["anchors"][1], result["lines"]) == 1


# ---------------------------------------------------------------------------
# ShellOperationsLayer: content-derived anchors + edit delegation
#
# The prior `WriteInvalidatesAnchors` behavior is dropped: anchors are now
# stateless and content-derived, so `write` has nothing to invalidate. An anchor
# over changed content simply fails to resolve at edit time — covered by the
# anchored-editing conflict scenarios.
# ---------------------------------------------------------------------------


class TestContentDerivedAnchors:
    @pytest.mark.asyncio
    async def test_cat_returns_resolvable_content_anchors(self, env):
        """cat returns content-derived anchors keyed by absolute index, no shared state."""
        vfs_obj, session, ns, agent = env
        await vfs_obj.write(ns.id, "/t.txt", b"l0\nl1\nl2", principal_id=agent.id)
        fs_ops = fs_operations_for(session, ResourceLimits())
        result = await fs_ops.cat("/t.txt")
        assert result["error"] is None
        assert set(result["anchors"]) == {0, 1, 2}
        for idx, anchor in result["anchors"].items():
            assert resolve_anchor(anchor, result["lines"]) == idx

    @pytest.mark.asyncio
    async def test_tail_anchors_are_file_absolute(self, env):
        """tail anchors carry file-absolute indices, not slice-relative ones."""
        vfs_obj, session, ns, agent = env
        lines = [f"line{i}" for i in range(6)]
        await vfs_obj.write(ns.id, "/t.txt", "\n".join(lines).encode(), principal_id=agent.id)
        fs_ops = fs_operations_for(session, ResourceLimits())
        result = await fs_ops.tail("/t.txt", 3)
        assert set(result["anchors"]) == {3, 4, 5}


class TestEditDelegatesToAnchoredEditing:
    """ShellOperationsLayer/EditDelegatesToAnchoredEditing."""

    @pytest.mark.asyncio
    async def test_edit_round_trips_through_capability(self, env):
        """edit delegates to anchored-editing and writes a new version; returns version only."""
        vfs_obj, session, ns, agent = env
        await vfs_obj.write(ns.id, "/t.txt", b"l0\nl1\nl2", principal_id=agent.id)
        fs_ops = fs_operations_for(session, ResourceLimits())
        cat = await fs_ops.cat("/t.txt")
        result = await fs_ops.edit("/t.txt", cat["anchors"][1], cat["anchors"][1], ["NEW"])
        assert result == {"version_number": 2}
        again = await fs_ops.cat("/t.txt")
        assert again["lines"] == ["l0", "NEW", "l2"]

    @pytest.mark.asyncio
    async def test_edit_stale_anchor_conflicts(self, env):
        """An edit whose anchor no longer matches current content conflicts (no write)."""
        vfs_obj, session, ns, agent = env
        await vfs_obj.write(ns.id, "/t.txt", b"l0\nl1\nl2", principal_id=agent.id)
        fs_ops = fs_operations_for(session, ResourceLimits())
        cat = await fs_ops.cat("/t.txt")
        stale_anchor = cat["anchors"][1]
        # Concurrent change invalidates the captured anchor's content.
        await fs_ops.write("/t.txt", b"l0\nCHANGED\nl2")
        with pytest.raises(AnchorConflictError):
            await fs_ops.edit("/t.txt", stale_anchor, stale_anchor, ["x"])


# ---------------------------------------------------------------------------
# Reviewer finding 1: write() returns marshalable dict
# ---------------------------------------------------------------------------


class TestWriteReturnsMarshalableDict:
    """write() must return a plain dict, not a raw pydantic VersionMeta.

    Monty raises TypeError if the return value of an external function is a
    pydantic model it cannot serialise — even when the sandbox ignores the
    value.  The side-effect (file written) is still committed, leaving the
    agent with a spurious failure after a successful mutation.
    """

    @pytest.mark.asyncio
    async def test_write_returns_dict_with_version_and_size(self, env):
        """write() return value is a plain dict with version_number and size."""
        vfs_obj, session, ns, agent = env

        limits = ResourceLimits()
        fs_ops = fs_operations_for(session, limits)

        content = b"hello world\n"
        result = await fs_ops.write("/new_file.txt", content)

        assert isinstance(result, dict), f"Expected dict, got {type(result)}"
        assert "version_number" in result
        assert "size" in result
        assert isinstance(result["version_number"], int)
        assert result["size"] == len(content)

    @pytest.mark.asyncio
    async def test_write_then_cat_roundtrips(self, env):
        """write() dict return does not prevent reading the content back."""
        vfs_obj, session, ns, agent = env

        limits = ResourceLimits()
        fs_ops = fs_operations_for(session, limits)

        content = b"line one\nline two\n"
        await fs_ops.write("/rw.txt", content)

        cat_result = await fs_ops.cat("/rw.txt")
        assert cat_result["error"] is None
        assert cat_result["lines"] == ["line one", "line two", ""]


# ---------------------------------------------------------------------------
# Reviewer finding 4: ls synthesizes directory entries
# ---------------------------------------------------------------------------


class TestLsSynthesizesDirectories:
    """ShellOperationsLayer/LsSynthesizesDirectories: non-recursive ls must
    emit synthesized is_dir=True entries for implicit subdirectories.
    """

    @pytest.mark.asyncio
    async def test_ls_synthesizes_subdir_entry(self, env):
        """ls(path) returns a synthesized dir entry for any immediate subdirectory."""
        vfs_obj, session, ns, agent = env

        await vfs_obj.write(ns.id, "/proj/file.txt", b"top-level file", principal_id=agent.id)
        await vfs_obj.write(ns.id, "/proj/sub/deep.txt", b"nested file", principal_id=agent.id)

        limits = ResourceLimits()
        fs_ops = fs_operations_for(session, limits)

        result = await fs_ops.ls("/proj/")
        entries = result["entries"]
        paths = {e["path"] for e in entries}

        # File at depth 1 should appear as a file entry
        assert "/proj/file.txt" in paths
        file_entry = next(e for e in entries if e["path"] == "/proj/file.txt")
        assert file_entry["is_dir"] is False

        # Subdirectory should appear as a synthesized dir entry
        assert "/proj/sub/" in paths
        dir_entry = next(e for e in entries if e["path"] == "/proj/sub/")
        assert dir_entry["is_dir"] is True
        assert dir_entry["name"] == "sub"
        # Synthesized dirs carry None for VFS-owned fields
        assert dir_entry["version_number"] is None
        assert dir_entry["updated_at"] is None

    @pytest.mark.asyncio
    async def test_ls_deduplicates_synthesized_dirs(self, env):
        """Multiple files under the same subdir produce exactly one synthesized entry."""
        vfs_obj, session, ns, agent = env

        await vfs_obj.write(ns.id, "/root/sub/a.txt", b"a", principal_id=agent.id)
        await vfs_obj.write(ns.id, "/root/sub/b.txt", b"b", principal_id=agent.id)

        limits = ResourceLimits()
        fs_ops = fs_operations_for(session, limits)

        result = await fs_ops.ls("/root/")
        dir_entries = [e for e in result["entries"] if e["is_dir"]]
        assert len(dir_entries) == 1
        assert dir_entries[0]["path"] == "/root/sub/"

    @pytest.mark.asyncio
    async def test_ls_deep_nesting_synthesizes_only_immediate_child(self, env):
        """Deeply nested paths produce only the immediate child dir entry."""
        vfs_obj, session, ns, agent = env

        await vfs_obj.write(ns.id, "/tree/a/b/c/leaf.txt", b"deep", principal_id=agent.id)

        limits = ResourceLimits()
        fs_ops = fs_operations_for(session, limits)

        result = await fs_ops.ls("/tree/")
        paths = {e["path"] for e in result["entries"]}

        # Only "/tree/a/" should appear; "/tree/a/b/" should NOT
        assert "/tree/a/" in paths
        assert "/tree/a/b/" not in paths


# ---------------------------------------------------------------------------
# Reviewer finding 5: max_read_bytes enforced before blob fetch
# ---------------------------------------------------------------------------


class TestOversizedStatBeforeRead:
    """max_read_bytes check must short-circuit before session.read is called.

    The 'no host OOM' claim requires that the blob is never fetched for
    files that exceed the limit.  This test instruments session.read with a
    counting wrapper and asserts it is never invoked.
    """

    @pytest.mark.asyncio
    async def test_cat_does_not_call_read_when_oversized(self, env):
        """cat returns oversized_read error WITHOUT calling session.read."""
        vfs_obj, session, ns, agent = env

        content = b"x" * 500
        await vfs_obj.write(ns.id, "/big.txt", content, principal_id=agent.id)

        read_call_count = 0
        original_read = session.read

        async def _counting_read(path, **kwargs):
            nonlocal read_call_count
            read_call_count += 1
            return await original_read(path, **kwargs)

        session.read = _counting_read

        limits = ResourceLimits(max_read_bytes=100)
        fs_ops = fs_operations_for(session, limits)

        result = await fs_ops.cat("/big.txt")

        assert result["error"] is not None
        assert result["error"]["code"] == "oversized_read"
        assert read_call_count == 0, (
            f"session.read was called {read_call_count} time(s); "
            "it must not be called when the stat-based pre-check fires"
        )

    @pytest.mark.asyncio
    async def test_head_does_not_call_read_when_oversized(self, env):
        """head returns oversized_read error WITHOUT calling session.read."""
        vfs_obj, session, ns, agent = env

        content = b"line\n" * 200  # 1000 bytes
        await vfs_obj.write(ns.id, "/big.txt", content, principal_id=agent.id)

        read_call_count = 0
        original_read = session.read

        async def _counting_read(path, **kwargs):
            nonlocal read_call_count
            read_call_count += 1
            return await original_read(path, **kwargs)

        session.read = _counting_read

        limits = ResourceLimits(max_read_bytes=50)
        fs_ops = fs_operations_for(session, limits)

        result = await fs_ops.head("/big.txt", 5)

        assert result["error"] is not None
        assert result["error"]["code"] == "oversized_read"
        assert read_call_count == 0


# ---------------------------------------------------------------------------
# Reviewer finding 7: grep recursive param honored
# ---------------------------------------------------------------------------


class TestGrepRecursiveParam:
    """grep(pattern, path, recursive=False) must exclude subdirectory results."""

    @pytest.mark.asyncio
    async def test_grep_nonrecursive_excludes_subdirs(self, env):
        """grep with recursive=False returns only depth-1 matches."""
        vfs_obj, session, ns, agent = env

        await vfs_obj.write(ns.id, "/dir/top.py", b"def hello(): pass", principal_id=agent.id)
        await vfs_obj.write(ns.id, "/dir/sub/nested.py", b"def hello(): pass", principal_id=agent.id)

        limits = ResourceLimits()
        fs_ops = fs_operations_for(session, limits)

        result = await fs_ops.grep("hello", "/dir/", recursive=False)
        paths = {r["path"] for r in result["results"]}

        assert "/dir/top.py" in paths
        assert "/dir/sub/nested.py" not in paths

    @pytest.mark.asyncio
    async def test_grep_recursive_includes_subdirs(self, env):
        """grep with recursive=True (default) returns matches at all depths."""
        vfs_obj, session, ns, agent = env

        await vfs_obj.write(ns.id, "/dir2/top.py", b"def hello(): pass", principal_id=agent.id)
        await vfs_obj.write(ns.id, "/dir2/sub/nested.py", b"def hello(): pass", principal_id=agent.id)

        limits = ResourceLimits()
        fs_ops = fs_operations_for(session, limits)

        result = await fs_ops.grep("hello", "/dir2/", recursive=True)
        paths = {r["path"] for r in result["results"]}

        # Both files should appear
        assert "/dir2/top.py" in paths
        assert "/dir2/sub/nested.py" in paths
