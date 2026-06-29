"""Unit tests for the stateless anchored-editing capability.

Covers the scenarios in ``specs/anchored-editing``:
  AnchorIdentity/IndexTargetsUniqueLineEvenInBoilerplate
  AnchorIdentity/AnchorReproducibleAcrossIndependentCalls
  AnchorIdentity/ChecksumBindsIndexToContent
  AnchoredRead/ReadReturnsContentVersionAndAnchors
  AnchoredRead/WindowedReadUsesAbsoluteIndices
  AnchoredRead/EmptyAndSingleLineFiles
  AnchoredRead/UndecodableContentRaises
  AnchoredRead/CrlfAndTrailingNewlinePreserved
  AnchoredEdit/SingleHunkApplies
  AnchoredEdit/MultipleHunksAppliedAtomically
  AnchoredEdit/ResultCarriesNoContentOrAnchors
  AnchoredEditConflicts/FileChangedSinceReadConflicts
  AnchoredEditConflicts/ChecksumMismatchConflicts
  AnchoredEditConflicts/OutOfRangeOrInvertedConflicts
  AnchoredEditConflicts/EditTombstonedFileConflicts
  ConsistencyFloor/StaleReadManifestsAsConflict
  ConsistencyFloor/ConcurrentEditsSerialize
  AnchoredEditingStandaloneSurface/StandaloneReadEditCycle
  AnchoredEditingStandaloneSurface/EditRequiresWritePermission

The old per-``execute`` ``AnchorMap`` (token pool, difflib reconciliation,
invalidate-on-write) is removed; its requirement was promoted and redesigned as
this stateless, content-derived capability, so the prior tests no longer apply.
"""

from __future__ import annotations

import dataclasses

import pytest
import pytest_asyncio

from vfs.anchored_editing import AnchoredEditor, Hunk, anchors_for_lines, make_anchor, resolve_anchor
from vfs.anchored_editing.editor import _decode
from vfs.config import VFSConfig
from vfs.errors import AnchorConflictError, ContentDecodeError, NotFoundError, PermissionDeniedError
from vfs.session import Session
from vfs.vfs import VFS

# ---------------------------------------------------------------------------
# Fixtures (real SQLite + local-FS blob, mirroring test_fs_ops.py)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def vfs_inst(tmp_path):
    config = VFSConfig(
        metadata_store_uri=f"sqlite:///{tmp_path / 'test.db'}",
        blob_store_uri=f"file:///{tmp_path / 'blobs'}/",
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
    """Namespace + a full-access principal; returns (vfs, ns, admin, session, editor)."""
    ns = await vfs_inst.create_namespace("testns", "admin")
    admin = await vfs_inst.create_principal("admin-user")
    await vfs_inst.bootstrap_admin(admin.id, ns.id)
    agent = await vfs_inst.create_principal("agent-user")
    await vfs_inst.grant(admin.id, agent.id, ns.id, "/", {"read", "write", "delete", "admin"})
    session = Session(vfs_inst, ns.id, agent.id)
    return vfs_inst, ns, admin, session, AnchoredEditor(session)


async def _write(session: Session, path: str, text: str) -> int:
    vm = await session.write(path, text.encode("utf-8"))
    return vm.version_number


# ---------------------------------------------------------------------------
# Pure anchor primitive
# ---------------------------------------------------------------------------


class TestAnchorIdentity:
    def test_index_targets_unique_line_even_in_boilerplate(self):
        """Identical-boilerplate lines are uniquely targetable by absolute index."""
        lines = ["import x", "", "", "", "import y"]  # three identical blank lines
        anchors = anchors_for_lines(lines)
        # Each blank line gets a distinct anchor (index differs); each resolves to itself.
        assert anchors[1] != anchors[2] != anchors[3]
        assert resolve_anchor(anchors[1], lines) == 1
        assert resolve_anchor(anchors[2], lines) == 2
        assert resolve_anchor(anchors[3], lines) == 3

    def test_anchor_reproducible_across_independent_calls(self):
        """An anchor is pure content function — no shared state needed to resolve it."""
        lines = ["alpha", "beta", "gamma"]
        anchor = make_anchor(1, lines[1])
        # A separately-produced anchor for the same (index, content) is identical.
        assert anchor == make_anchor(1, "beta")
        assert resolve_anchor(anchor, lines) == 1

    def test_checksum_binds_index_to_content(self):
        """Altering an anchor's index without recomputing the checksum is detectable."""
        lines = ["aaa", "bbb", "ccc"]
        anchor = make_anchor(1, lines[1])  # "1:<ck>"
        _, ck = anchor.split(":")
        forged = f"2:{ck}"  # point at a different line, keep the old checksum
        with pytest.raises(AnchorConflictError):
            resolve_anchor(forged, lines)

    def test_checksum_distinguishes_identical_lines_by_index(self):
        """Index-bound checksum: identical content at different indices differs."""
        assert make_anchor(47, "x = 1") != make_anchor(48, "x = 1")


# ---------------------------------------------------------------------------
# AnchoredRead
# ---------------------------------------------------------------------------


class TestAnchoredRead:
    @pytest.mark.asyncio
    async def test_read_returns_content_version_and_anchors(self, env):
        _, _, _, _, editor = env
        _, _, _, session, _ = env
        v = await _write(session, "/f.txt", "l0\nl1\nl2")
        res = await editor.read_anchored("/f.txt")
        assert res.version == v
        assert res.lines == ["l0", "l1", "l2"]
        assert set(res.anchors) == {0, 1, 2}
        assert resolve_anchor(res.anchors[1], res.lines) == 1

    @pytest.mark.asyncio
    async def test_windowed_read_uses_absolute_indices(self, env):
        _, _, _, session, editor = env
        body = "\n".join(f"line{i}" for i in range(200))
        await _write(session, "/big.txt", body)
        full = await editor.read_anchored("/big.txt")
        window = await editor.read_anchored("/big.txt", offset=100, limit=10)
        assert window.offset == 100
        assert set(window.anchors) == set(range(100, 110))
        # Absolute anchors match the full read for the same lines.
        for i in range(100, 110):
            assert window.anchors[i] == full.anchors[i]

    @pytest.mark.asyncio
    async def test_empty_and_single_line_files(self, env):
        _, _, _, session, editor = env
        await _write(session, "/empty.txt", "")
        await _write(session, "/one.txt", "only")
        empty = await editor.read_anchored("/empty.txt")
        one = await editor.read_anchored("/one.txt")
        assert empty.lines == [""] and set(empty.anchors) == {0}
        assert one.lines == ["only"] and set(one.anchors) == {0}

    @pytest.mark.asyncio
    async def test_undecodable_content_raises(self, env):
        _, _, _, session, editor = env
        await session.write("/bin.dat", b"\xff\xfe\x00\x01")
        with pytest.raises(ContentDecodeError):
            await editor.read_anchored("/bin.dat")

    @pytest.mark.asyncio
    async def test_crlf_and_trailing_newline_preserved(self, env):
        """A read→edit round-trip preserves \\r and a missing trailing newline."""
        _, vfs_inst, _, session, editor = env
        _, ns, _, _, _ = env
        # CRLF line endings, no trailing newline.
        original = "a\r\nb\r\nc"
        v = await _write(session, "/crlf.txt", original)
        res = await editor.read_anchored("/crlf.txt")
        assert res.lines == ["a\r", "b\r", "c"]  # \r retained, no trailing "" element
        # Replace the middle line, keeping its \r; the round-trip must not add/remove a newline.
        hunk = Hunk(res.anchors[1], res.anchors[1], ["B\r"])
        await editor.edit_anchored("/crlf.txt", [hunk], expected_version=v)
        content = (await session.read("/crlf.txt")).decode("utf-8")
        assert content == "a\r\nB\r\nc"


# ---------------------------------------------------------------------------
# AnchoredEdit
# ---------------------------------------------------------------------------


class TestAnchoredEdit:
    @pytest.mark.asyncio
    async def test_single_hunk_applies(self, env):
        _, _, _, session, editor = env
        v = await _write(session, "/f.txt", "l0\nl1\nl2\nl3\nl4\nl5\nl6")
        res = await editor.read_anchored("/f.txt")
        # Replace inclusive lines 3..5 with two new lines.
        hunk = Hunk(res.anchors[3], res.anchors[5], ["X", "Y"])
        edit = await editor.edit_anchored("/f.txt", [hunk], expected_version=v)
        assert edit.new_version == v + 1
        content = (await session.read("/f.txt")).decode("utf-8")
        assert content == "l0\nl1\nl2\nX\nY\nl6"

    @pytest.mark.asyncio
    async def test_multiple_hunks_applied_atomically(self, env):
        _, _, _, session, editor = env
        v = await _write(session, "/f.txt", "l0\nl1\nl2\nl3\nl4\nl5")
        res = await editor.read_anchored("/f.txt")
        h1 = Hunk(res.anchors[0], res.anchors[0], ["A"])
        h2 = Hunk(res.anchors[4], res.anchors[4], ["E"])
        edit = await editor.edit_anchored("/f.txt", [h1, h2], expected_version=v)
        assert edit.new_version == v + 1
        content = (await session.read("/f.txt")).decode("utf-8")
        assert content == "A\nl1\nl2\nl3\nE\nl5"

    @pytest.mark.asyncio
    async def test_failed_hunk_aborts_whole_edit(self, env):
        _, _, _, session, editor = env
        v = await _write(session, "/f.txt", "l0\nl1\nl2")
        res = await editor.read_anchored("/f.txt")
        good = Hunk(res.anchors[0], res.anchors[0], ["A"])
        bad = Hunk("99:000", "99:000", ["Z"])  # out of range
        with pytest.raises(AnchorConflictError):
            await editor.edit_anchored("/f.txt", [good, bad], expected_version=v)
        # Nothing written: still at version v.
        assert (await session.stat("/f.txt")).current_version_number == v

    @pytest.mark.asyncio
    async def test_result_carries_no_content_or_anchors(self, env):
        _, _, _, session, editor = env
        v = await _write(session, "/f.txt", "l0\nl1")
        res = await editor.read_anchored("/f.txt")
        edit = await editor.edit_anchored("/f.txt", [Hunk(res.anchors[0], res.anchors[0], ["x"])], expected_version=v)
        field_names = {f.name for f in dataclasses.fields(edit)}
        assert field_names == {"new_version"}


# ---------------------------------------------------------------------------
# AnchoredEditConflicts
# ---------------------------------------------------------------------------


class TestAnchoredEditConflicts:
    @pytest.mark.asyncio
    async def test_file_changed_since_read_conflicts(self, env):
        _, _, _, session, editor = env
        v = await _write(session, "/f.txt", "l0\nl1\nl2")
        res = await editor.read_anchored("/f.txt")
        # Concurrent write advances the file.
        await _write(session, "/f.txt", "l0\nCHANGED\nl2")
        with pytest.raises(AnchorConflictError):
            await editor.edit_anchored("/f.txt", [Hunk(res.anchors[1], res.anchors[1], ["x"])], expected_version=v)
        # The concurrent write was not overwritten.
        assert (await session.read("/f.txt")).decode("utf-8") == "l0\nCHANGED\nl2"

    @pytest.mark.asyncio
    async def test_checksum_mismatch_conflicts(self, env):
        """An anchor whose index/checksum disagrees with current content is rejected."""
        _, _, _, session, editor = env
        v = await _write(session, "/f.txt", "same\nsame\nother")
        res = await editor.read_anchored("/f.txt")
        # Transpose: use line-1's checksum but point it at index 2 (different content).
        _, ck1 = res.anchors[1].split(":")
        forged = f"2:{ck1}"
        with pytest.raises(AnchorConflictError):
            await editor.edit_anchored("/f.txt", [Hunk(forged, forged, ["x"])], expected_version=v)
        assert (await session.stat("/f.txt")).current_version_number == v

    @pytest.mark.asyncio
    async def test_out_of_range_index_conflicts(self, env):
        _, _, _, session, editor = env
        v = await _write(session, "/f.txt", "l0\nl1")
        with pytest.raises(AnchorConflictError):
            await editor.edit_anchored("/f.txt", [Hunk("50:abc", "50:abc", ["x"])], expected_version=v)

    @pytest.mark.asyncio
    async def test_inverted_range_conflicts(self, env):
        _, _, _, session, editor = env
        v = await _write(session, "/f.txt", "l0\nl1\nl2\nl3")
        res = await editor.read_anchored("/f.txt")
        with pytest.raises(AnchorConflictError):
            # end before start
            await editor.edit_anchored("/f.txt", [Hunk(res.anchors[3], res.anchors[1], ["x"])], expected_version=v)

    @pytest.mark.asyncio
    async def test_overlapping_hunks_conflict(self, env):
        _, _, _, session, editor = env
        v = await _write(session, "/f.txt", "l0\nl1\nl2\nl3")
        res = await editor.read_anchored("/f.txt")
        h1 = Hunk(res.anchors[0], res.anchors[2], ["x"])
        h2 = Hunk(res.anchors[1], res.anchors[3], ["y"])
        with pytest.raises(AnchorConflictError):
            await editor.edit_anchored("/f.txt", [h1, h2], expected_version=v)

    @pytest.mark.asyncio
    async def test_edit_tombstoned_file_conflicts(self, env):
        _, _, _, session, editor = env
        v = await _write(session, "/f.txt", "l0\nl1")
        res = await editor.read_anchored("/f.txt")
        await session.delete("/f.txt")
        with pytest.raises((AnchorConflictError, NotFoundError)):
            await editor.edit_anchored("/f.txt", [Hunk(res.anchors[0], res.anchors[0], ["x"])], expected_version=v)


# ---------------------------------------------------------------------------
# ConsistencyFloor
# ---------------------------------------------------------------------------


class TestConsistencyFloor:
    @pytest.mark.asyncio
    async def test_stale_read_manifests_as_conflict(self, env, monkeypatch):
        """A stale read whose up-front version check passes is still caught by the CAS write."""
        _, _, _, session, editor = env
        v = await _write(session, "/f.txt", "l0\nl1\nl2")
        res = await editor.read_anchored("/f.txt")
        # Authoritative store advances to v+1.
        await _write(session, "/f.txt", "l0\nl1\nADVANCED")

        # Simulate a stale read: stat reports the OLD version, so the up-front
        # check passes — only the CAS write rejects the edit.
        real_stat = session.stat

        async def stale_stat(path: str):
            meta = await real_stat(path)
            return meta.model_copy(update={"current_version_number": v})

        monkeypatch.setattr(session, "stat", stale_stat)
        with pytest.raises(AnchorConflictError):
            await editor.edit_anchored("/f.txt", [Hunk(res.anchors[0], res.anchors[0], ["x"])], expected_version=v)
        # The authoritative content is untouched.
        assert (await real_stat("/f.txt")).current_version_number == v + 1

    @pytest.mark.asyncio
    async def test_concurrent_edits_serialize(self, env):
        _, _, _, session, editor = env
        v = await _write(session, "/f.txt", "l0\nl1\nl2")
        res = await editor.read_anchored("/f.txt")
        # Both edits derive from the same version; exactly one wins.
        first = await editor.edit_anchored(
            "/f.txt", [Hunk(res.anchors[0], res.anchors[0], ["FIRST"])], expected_version=v
        )
        assert first.new_version == v + 1
        with pytest.raises(AnchorConflictError):
            await editor.edit_anchored(
                "/f.txt", [Hunk(res.anchors[2], res.anchors[2], ["SECOND"])], expected_version=v
            )


# ---------------------------------------------------------------------------
# AnchoredEditingStandaloneSurface
# ---------------------------------------------------------------------------


class TestStandaloneSurface:
    @pytest.mark.asyncio
    async def test_standalone_read_edit_cycle(self, env):
        """read_anchored → edit_anchored as two independent calls, no sandbox."""
        _, _, _, session, editor = env
        v = await _write(session, "/f.txt", "keep\nreplace\nkeep")
        read = await editor.read_anchored("/f.txt")
        edit = await editor.edit_anchored(
            "/f.txt", [Hunk(read.anchors[1], read.anchors[1], ["REPLACED"])], expected_version=read.version
        )
        assert edit.new_version == v + 1
        assert (await session.read("/f.txt")).decode("utf-8") == "keep\nREPLACED\nkeep"

    @pytest.mark.asyncio
    async def test_edit_requires_write_permission(self, vfs_inst):
        ns = await vfs_inst.create_namespace("ns2", "admin")
        admin = await vfs_inst.create_principal("admin2")
        await vfs_inst.bootstrap_admin(admin.id, ns.id)
        # Author the file via a full-access principal (bootstrap grants only `admin`).
        author = await vfs_inst.create_principal("author")
        await vfs_inst.grant(admin.id, author.id, ns.id, "/", {"read", "write", "delete"})
        v = await _write(Session(vfs_inst, ns.id, author.id), "/f.txt", "l0\nl1")
        # Reader principal: read only.
        reader = await vfs_inst.create_principal("reader")
        await vfs_inst.grant(admin.id, reader.id, ns.id, "/", {"read"})
        reader_session = Session(vfs_inst, ns.id, reader.id)
        reader_editor = AnchoredEditor(reader_session)
        read = await reader_editor.read_anchored("/f.txt")  # read allowed
        with pytest.raises(PermissionDeniedError):
            await reader_editor.edit_anchored(
                "/f.txt", [Hunk(read.anchors[0], read.anchors[0], ["x"])], expected_version=v
            )


def test_decode_helper_rejects_binary():
    with pytest.raises(ContentDecodeError):
        _decode(b"\xff\xfe")
