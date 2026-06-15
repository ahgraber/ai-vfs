"""Unit tests for AnchorMap and edit() shell wrapper.

Covers every scenario in the AnchoredEditing requirement:
  AnchoredEditing/SingleTokenPoolFirst
  AnchoredEditing/ValidateKnownAnchor
  AnchoredEditing/ValidateWrongPathConflict
  AnchoredEditing/StaleVersionConflict
  AnchoredEditing/StaleLineContentConflict
  AnchoredEditing/SuccessfulEditReturnsUpdatedAnchors
  AnchoredEditing/CasConflictSurfacesAsAnchorConflict
  AnchoredEditing/InvalidatedAnchorRejected
  AnchoredEditing/EditReconcilesAnchorsAtomically
  AnchoredEditing/MyersDiffPreservesUnchangedAnchors
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from vfs.config import VFSConfig
from vfs.errors import AnchorConflictError, ConflictError
from vfs.execution.anchors import _POOL, _POOL_SET, AnchorMap
from vfs.execution.fs_ops import fs_operations_for
from vfs.protocols.execution import ResourceLimits
from vfs.session import Session
from vfs.vfs import VFS

# ---------------------------------------------------------------------------
# Fixtures (mirror test_fs_ops.py pattern: real SQLite + local-FS blob)
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
async def env(vfs_inst, tmp_path):
    """Namespace + principal with full access; returns (vfs, session, ns, agent, amap, fs_ops)."""
    ns = await vfs_inst.create_namespace("testns", "admin")
    admin = await vfs_inst.create_principal("admin-user")
    await vfs_inst.bootstrap_admin(admin.id, ns.id)
    agent = await vfs_inst.create_principal("agent-user")
    await vfs_inst.grant(admin.id, agent.id, ns.id, "/", {"read", "write", "delete", "admin"})

    session = Session(vfs_inst, ns.id, agent.id)
    amap = AnchorMap()
    limits = ResourceLimits()
    fs_ops = fs_operations_for(session, limits, anchor_map=amap)
    return vfs_inst, session, ns, agent, amap, fs_ops


# ---------------------------------------------------------------------------
# AnchoredEditing/SingleTokenPoolFirst
# ---------------------------------------------------------------------------


class TestSingleTokenPoolFirst:
    """AnchoredEditing/SingleTokenPoolFirst: pool entries are used before fallback."""

    def test_first_allocations_use_pool(self):
        """Fresh AnchorMap: first tokens come from the single-token pool."""
        amap = AnchorMap()
        anchors = amap.allocate("/a.py", 1, ["line0", "line1", "line2", "line3", "line4"])
        tokens = list(anchors.values())
        # All five tokens must be present in the pool (not fallback strings).
        for tok in tokens:
            assert tok in _POOL_SET, f"Token {tok!r} is not in the pool (expected pool entry)"

    def test_pool_entries_are_ascii_identifier_safe(self):
        """Pool entries must be ASCII and identifier-safe (no non-ASCII chars)."""
        for tok in _POOL[:50]:  # spot-check first 50
            assert tok.isascii(), f"Pool token {tok!r} contains non-ASCII characters"
            assert tok.replace("_", "").isalnum(), f"Pool token {tok!r} is not identifier-safe"

    def test_pool_size_is_1700(self):
        """Pool has exactly 1700 entries (676 two-char + 1024 three-char)."""
        assert len(_POOL) == 1700

    def test_two_char_entries_come_first(self):
        """First 676 entries are all two-character strings."""
        two_char = _POOL[:676]
        assert all(len(t) == 2 for t in two_char)

    def test_three_char_entries_follow(self):
        """Entries 676–1699 are all three-character strings."""
        three_char = _POOL[676:]
        assert len(three_char) == 1024
        assert all(len(t) == 3 for t in three_char)


# ---------------------------------------------------------------------------
# AnchoredEditing/ValidateKnownAnchor
# ---------------------------------------------------------------------------


class TestValidateKnownAnchor:
    """AnchoredEditing/ValidateKnownAnchor."""

    def test_returns_version_and_content(self):
        """validate returns (version_number, line_content) for a known anchor."""
        amap = AnchorMap()
        lines = ["  pass", "  return x", "  raise"]
        anchors = amap.allocate("/src/a.py", 2, lines)
        tok = anchors[1]  # token for line index 1 ("  return x")
        version, content = amap.validate(tok, "/src/a.py")
        assert version == 2
        assert content == "  return x"


# ---------------------------------------------------------------------------
# AnchoredEditing/ValidateWrongPathConflict
# ---------------------------------------------------------------------------


class TestValidateWrongPathConflict:
    """AnchoredEditing/ValidateWrongPathConflict."""

    def test_different_path_raises_anchor_conflict(self):
        """validate raises AnchorConflictError when path does not match anchor's path."""
        amap = AnchorMap()
        anchors = amap.allocate("/src/a.py", 1, ["line0"])
        tok = anchors[0]
        with pytest.raises(AnchorConflictError):
            amap.validate(tok, "/src/b.py")

    def test_unknown_token_raises_anchor_conflict(self):
        """validate raises AnchorConflictError for a token that was never allocated."""
        amap = AnchorMap()
        with pytest.raises(AnchorConflictError):
            amap.validate("NONEXISTENT", "/src/a.py")


# ---------------------------------------------------------------------------
# AnchoredEditing/StaleVersionConflict
# ---------------------------------------------------------------------------


class TestStaleVersionConflict:
    """AnchoredEditing/StaleVersionConflict: stat pre-check rejects stale anchors."""

    @pytest.mark.asyncio
    async def test_stale_version_raises_anchor_conflict(self, env):
        """edit() raises AnchorConflictError when file version advanced past anchor version."""
        vfs_obj, session, ns, agent, amap, fs_ops = env

        # Write initial version
        content = "line0\nline1\nline2\n"
        await vfs_obj.write(ns.id, "/src/a.py", content.encode(), principal_id=agent.id)

        # Allocate anchors via cat (captures version 1)
        cat_result = await fs_ops.cat("/src/a.py")
        assert cat_result["error"] is None
        anchors = cat_result["anchors"]
        start_tok = anchors[0]
        end_tok = anchors[1]

        # Advance the file to version 2 via a raw write (bypasses anchor invalidation
        # to simulate a concurrent external write — use vfs directly to skip amap.invalidate)
        await vfs_obj.write(ns.id, "/src/a.py", b"line0\nline1\nline2\n", principal_id=agent.id)

        # edit() must detect version mismatch via stat pre-check
        with pytest.raises(AnchorConflictError):
            await fs_ops.edit("/src/a.py", start_tok, end_tok, ["replacement"])


# ---------------------------------------------------------------------------
# AnchoredEditing/StaleLineContentConflict
# ---------------------------------------------------------------------------


class TestStaleLineContentConflict:
    """AnchoredEditing/StaleLineContentConflict: line-content check rejects shifted lines."""

    @pytest.mark.asyncio
    async def test_stale_line_content_raises_anchor_conflict(self, env):
        """edit() raises AnchorConflictError when a concurrent edit shifted line content
        while keeping the version number the same (simulated by patching the anchor entry).
        """
        vfs_obj, session, ns, agent, amap, fs_ops = env

        content = "line0\nline1\nline2\n"
        await vfs_obj.write(ns.id, "/src/b.py", content.encode(), principal_id=agent.id)

        cat_result = await fs_ops.cat("/src/b.py")
        assert cat_result["error"] is None
        anchors = cat_result["anchors"]
        start_tok = anchors[0]
        end_tok = anchors[1]

        # Simulate stale content by mutating the anchor entry's line_content
        # without advancing the version (as if a concurrent write happened and
        # lines shifted but version stayed — we just corrupt the stored content
        # to trigger the line-content check path).
        amap._entries[start_tok].line_content = "DIFFERENT CONTENT"

        with pytest.raises(AnchorConflictError):
            await fs_ops.edit("/src/b.py", start_tok, end_tok, ["replacement"])


# ---------------------------------------------------------------------------
# AnchoredEditing/SuccessfulEditReturnsUpdatedAnchors
# ---------------------------------------------------------------------------


class TestSuccessfulEditReturnsUpdatedAnchors:
    """AnchoredEditing/SuccessfulEditReturnsUpdatedAnchors."""

    @pytest.mark.asyncio
    async def test_successful_edit_result_shape(self, env):
        """edit() succeeds, result has version_number, anchors, lines;
        lines 1–3 and 7–10 keep original tokens; lines 4–5 have new tokens.
        """
        vfs_obj, session, ns, agent, amap, fs_ops = env

        # 10-line file (0-indexed lines 0–9)
        lines = [f"line{i}" for i in range(10)]
        content = "\n".join(lines) + "\n"
        await vfs_obj.write(ns.id, "/src/c.py", content.encode(), principal_id=agent.id)

        cat_result = await fs_ops.cat("/src/c.py")
        assert cat_result["error"] is None
        anchors = cat_result["anchors"]  # {line_index: token}

        # Edit lines 3–5 (0-indexed) with 2 replacement lines
        start_tok = anchors[3]
        end_tok = anchors[5]
        replacement = ["replacement_a", "replacement_b"]

        result = await fs_ops.edit("/src/c.py", start_tok, end_tok, replacement)

        assert "version_number" in result
        assert "anchors" in result
        assert "lines" in result
        assert isinstance(result["version_number"], int)

        new_anchors = result["anchors"]  # {new_line_index: token}

        # Verify unchanged lines 0–2 kept their original tokens
        # After edit: lines 0-2 stay at indices 0-2
        for old_idx in range(3):
            old_tok = anchors[old_idx]
            assert new_anchors.get(old_idx) == old_tok, (
                f"Line {old_idx} should keep original token {old_tok!r}; got {new_anchors.get(old_idx)!r}"
            )

        # Lines 3–5 replaced by 2 lines → new_lines at indices 3 and 4
        # Those should have NEW tokens (different from any old token for those positions)
        old_replaced_tokens = {anchors[3], anchors[4], anchors[5]}
        for new_idx in [3, 4]:
            tok = new_anchors.get(new_idx)
            assert tok is not None, f"No anchor at new index {new_idx}"
            assert tok not in old_replaced_tokens, (
                f"Replaced line {new_idx} should have a new token, not one of {old_replaced_tokens!r}"
            )

        # Lines 6–9 (originally at indices 6–9) shift to indices 5–8
        for shift, old_idx in enumerate(range(6, 10)):
            new_idx = 5 + shift  # 5, 6, 7, 8
            old_tok = anchors[old_idx]
            assert new_anchors.get(new_idx) == old_tok, (
                f"Line originally at {old_idx} (now at {new_idx}) should keep token {old_tok!r}"
            )

        # Verify the written content is correct
        new_lines_expected = lines[:3] + replacement + lines[6:] + [""]
        assert result["lines"] == new_lines_expected


# ---------------------------------------------------------------------------
# AnchoredEditing/CasConflictSurfacesAsAnchorConflict
# ---------------------------------------------------------------------------


class TestCasConflictSurfacesAsAnchorConflict:
    """AnchoredEditing/CasConflictSurfacesAsAnchorConflict."""

    @pytest.mark.asyncio
    async def test_conflict_error_becomes_anchor_conflict(self, env, monkeypatch):
        """ConflictError from session.write is surfaced as AnchorConflictError."""
        vfs_obj, session, ns, agent, amap, fs_ops = env

        content = "line0\nline1\nline2\n"
        await vfs_obj.write(ns.id, "/src/d.py", content.encode(), principal_id=agent.id)

        cat_result = await fs_ops.cat("/src/d.py")
        assert cat_result["error"] is None
        anchors = cat_result["anchors"]
        start_tok = anchors[0]
        end_tok = anchors[1]

        # Patch session.write to raise ConflictError
        async def _raising_write(*args, **kwargs):
            raise ConflictError("CAS mismatch")

        monkeypatch.setattr(session, "write", _raising_write)

        with pytest.raises(AnchorConflictError):
            await fs_ops.edit("/src/d.py", start_tok, end_tok, ["new line"])


# ---------------------------------------------------------------------------
# AnchoredEditing/InvalidatedAnchorRejected
# ---------------------------------------------------------------------------


class TestInvalidatedAnchorRejected:
    """AnchoredEditing/InvalidatedAnchorRejected."""

    @pytest.mark.asyncio
    async def test_invalidated_anchor_raises_on_validate(self, env):
        """After raw write() invalidates a path, old anchors raise AnchorConflictError."""
        vfs_obj, session, ns, agent, amap, fs_ops = env

        await vfs_obj.write(ns.id, "/src/e.py", b"line0\nline1\n", principal_id=agent.id)

        cat_result = await fs_ops.cat("/src/e.py")
        assert cat_result["error"] is None
        old_tok = next(iter(cat_result["anchors"].values()))

        # Raw write through FsOperations — should call amap.invalidate("/src/e.py")
        await fs_ops.write("/src/e.py", b"new content\n")

        with pytest.raises(AnchorConflictError):
            amap.validate(old_tok, "/src/e.py")


# ---------------------------------------------------------------------------
# AnchoredEditing/EditReconcilesAnchorsAtomically
# ---------------------------------------------------------------------------


class TestEditReconcilesAnchorsAtomically:
    """AnchoredEditing/EditReconcilesAnchorsAtomically."""

    @pytest.mark.asyncio
    async def test_unchanged_anchors_valid_after_edit(self, env):
        """After edit(), unchanged-line anchors are valid with updated line_index and new version."""
        vfs_obj, session, ns, agent, amap, fs_ops = env

        lines = ["alpha", "beta", "gamma", "delta", "epsilon"]
        content = "\n".join(lines) + "\n"
        await vfs_obj.write(ns.id, "/src/f.py", content.encode(), principal_id=agent.id)

        cat_result = await fs_ops.cat("/src/f.py")
        assert cat_result["error"] is None
        anchors = cat_result["anchors"]

        start_tok = anchors[2]  # "gamma"
        end_tok = anchors[3]  # "delta"

        # Edit lines 2–3 (gamma, delta) with a single replacement
        result = await fs_ops.edit("/src/f.py", start_tok, end_tok, ["REPLACED"])
        new_version = result["version_number"]

        # Tokens for lines 0–1 ("alpha", "beta") must still be valid at new version
        for old_idx in [0, 1]:
            tok = anchors[old_idx]
            entry = amap._entries.get(tok)
            assert entry is not None, f"Anchor for line {old_idx} was removed; should be preserved"
            assert entry.version_number == new_version, (
                f"Unchanged anchor version {entry.version_number} != new version {new_version}"
            )
            assert entry.line_index == old_idx, f"Unchanged anchor line_index {entry.line_index} != expected {old_idx}"

        # Token for line 4 ("epsilon") should now be at index 3 (shifted by -1 net)
        tok_epsilon = anchors[4]
        entry_epsilon = amap._entries.get(tok_epsilon)
        assert entry_epsilon is not None, "Line 4 anchor removed; should be preserved"
        assert entry_epsilon.line_index == 3
        assert entry_epsilon.version_number == new_version

        # Old tokens for lines 2 and 3 (replaced) must be gone
        for replaced_idx in [2, 3]:
            old_tok = anchors[replaced_idx]
            assert old_tok not in amap._entries, f"Replaced-line token {old_tok!r} still present after reconcile"

    @pytest.mark.asyncio
    async def test_no_prior_invalidate_called(self, env, monkeypatch):
        """edit() does NOT call invalidate() — reconcile is the only state mutation."""
        vfs_obj, session, ns, agent, amap, fs_ops = env

        lines = ["x", "y", "z"]
        content = "\n".join(lines) + "\n"
        await vfs_obj.write(ns.id, "/src/g.py", content.encode(), principal_id=agent.id)

        cat_result = await fs_ops.cat("/src/g.py")
        anchors = cat_result["anchors"]

        invalidate_calls: list[str] = []
        original_invalidate = amap.invalidate

        def _spy_invalidate(path: str) -> None:
            invalidate_calls.append(path)
            original_invalidate(path)

        monkeypatch.setattr(amap, "invalidate", _spy_invalidate)

        start_tok = anchors[0]
        end_tok = anchors[0]
        await fs_ops.edit("/src/g.py", start_tok, end_tok, ["replaced"])

        # edit() must NOT have called invalidate
        assert invalidate_calls == [], f"edit() called invalidate() unexpectedly on: {invalidate_calls}"


# ---------------------------------------------------------------------------
# AnchoredEditing/MyersDiffPreservesUnchangedAnchors
# ---------------------------------------------------------------------------


class TestMyersDiffPreservesUnchangedAnchors:
    """AnchoredEditing/MyersDiffPreservesUnchangedAnchors.

    10-line file; reconcile called with lines 4–6 replaced by 2 new lines.
    Anchors for lines 1–3 and 7–10 (0-indexed 0–2 and 6–9) preserved.
    Anchors for lines 4–6 (0-indexed 3–5) replaced with new tokens.
    """

    def test_reconcile_preserves_and_replaces_correctly(self):
        """reconcile: unchanged lines keep tokens; replaced lines get new tokens."""
        amap = AnchorMap()

        old_lines = [f"line{i}" for i in range(10)]
        anchors = amap.allocate("/data.txt", 1, old_lines)
        old_tokens = dict(anchors)  # {line_idx: token}

        # Replace lines 3–5 (0-indexed) with 2 new lines
        new_lines = old_lines[:3] + ["new_a", "new_b"] + old_lines[6:]

        new_idx_to_token = amap.reconcile("/data.txt", old_lines, new_lines, version_number=2)

        # Lines 0–2: unchanged → same tokens at same indices
        for idx in range(3):
            assert new_idx_to_token.get(idx) == old_tokens[idx], (
                f"Line {idx} should keep original token {old_tokens[idx]!r}"
            )

        # Lines 3–4: new replacement lines → NEW tokens (not any of the old replaced tokens)
        old_replaced = {old_tokens[3], old_tokens[4], old_tokens[5]}
        for new_idx in [3, 4]:
            tok = new_idx_to_token.get(new_idx)
            assert tok is not None, f"No token at new index {new_idx}"
            assert tok not in old_replaced, f"Replaced line {new_idx} reused an old replaced token {tok!r}"

        # Lines 6–9 shift to indices 5–8 (replaced 3 lines with 2, net -1)
        for shift, old_idx in enumerate(range(6, 10)):
            new_idx = 5 + shift
            assert new_idx_to_token.get(new_idx) == old_tokens[old_idx], (
                f"Shifted line (old {old_idx} → new {new_idx}) should keep token {old_tokens[old_idx]!r}"
            )

        # Old tokens for lines 3–5 must be gone from the map
        for old_idx in [3, 4, 5]:
            assert old_tokens[old_idx] not in amap._entries, f"Deleted token {old_tokens[old_idx]!r} still in entries"

    def test_reconcile_updates_line_index_and_version(self):
        """reconcile updates line_index and version_number for preserved entries."""
        amap = AnchorMap()
        old_lines = ["a", "b", "c"]
        anchors = amap.allocate("/f.txt", 1, old_lines)

        # Prepend a line: all existing lines shift by +1
        new_lines = ["PREFIX"] + old_lines
        amap.reconcile("/f.txt", old_lines, new_lines, version_number=2)

        for old_idx, old_tok in anchors.items():
            entry = amap._entries.get(old_tok)
            assert entry is not None, f"Unchanged token {old_tok!r} was removed"
            assert entry.line_index == old_idx + 1, (
                f"line_index not updated: expected {old_idx + 1}, got {entry.line_index}"
            )
            assert entry.version_number == 2, f"version_number not updated: expected 2, got {entry.version_number}"

    def test_reconcile_duplicate_line_content_positional_caveat(self):
        """reconcile with duplicate line content: tokens follow content match, not fixed position.

        difflib.SequenceMatcher uses Ratcliff/Obershelp longest-common-block
        heuristics.  When a file has duplicate lines the matcher may assign an
        existing token to a *different* positional occurrence in the new file.
        This is the documented positional caveat for duplicate-content lines:
        the contract is that *some* token is present at each new index and that
        the total count of distinct tokens is correct — not that a specific
        occurrence maps to a specific token.
        """
        amap = AnchorMap()
        # File: ["a", "b", "a"] — "a" appears twice
        old_lines = ["a", "b", "a"]
        anchors = amap.allocate("/dup.txt", 1, old_lines)
        tok_a0 = anchors[0]  # token for first "a"
        tok_b = anchors[1]  # token for "b"
        tok_a2 = anchors[2]  # token for second "a"

        # Replace "b" with "c"; the two "a" lines are unchanged.
        new_lines = ["a", "c", "a"]
        new_map = amap.reconcile("/dup.txt", old_lines, new_lines, version_number=2)

        # "b" is replaced — its token must be gone.
        assert tok_b not in amap._entries, "Replaced 'b' token should be removed"

        # Both "a" occurrences survive (total 2 tokens for the two "a" positions).
        a_tokens_in_new = {new_map.get(0), new_map.get(2)}
        assert None not in a_tokens_in_new, "Both 'a' positions must have tokens after reconcile"
        # The set of tokens assigned to the two "a" positions is a subset of the
        # original "a" tokens — either or both may be reassigned per the matcher's
        # heuristic, but no new pool tokens are wasted on unchanged "a" lines.
        assert a_tokens_in_new <= {tok_a0, tok_a2}, (
            "Unchanged 'a' lines should reuse existing tokens (positional assignment may vary for duplicate content)"
        )

        # Position 1 ("c") gets a new token (not any of the old ones).
        c_tok = new_map.get(1)
        assert c_tok is not None
        assert c_tok not in {tok_a0, tok_b, tok_a2}, "Inserted 'c' line should receive a fresh token"


# ---------------------------------------------------------------------------
# Reviewer finding 2: tail anchors carry file-absolute line indices
# ---------------------------------------------------------------------------


class TestTailAnchorAbsoluteIndex:
    """tail() must allocate anchors with file-absolute line_index values.

    Reviewer reproduced: tail on a 6-line file stored line_index=0 for
    "line5" (the last line) because the allocator enumerated from 0 over
    the slice.  An edit via that anchor then targeted the WRONG line.
    """

    @pytest.mark.asyncio
    async def test_tail_anchor_line_index_is_file_absolute(self, env):
        """Anchors returned by tail() have file-absolute line_index values."""
        vfs_obj, session, ns, agent, amap, fs_ops = env

        lines = [f"line{i}" for i in range(6)]
        content = "\n".join(lines)  # no trailing newline → 6 lines
        await vfs_obj.write(ns.id, "/six.txt", content.encode(), principal_id=agent.id)

        tail_result = await fs_ops.tail("/six.txt", 3)
        assert tail_result["error"] is None
        assert tail_result["lines"] == ["line3", "line4", "line5"]

        # anchors dict is keyed by file-absolute line index (3, 4, 5)
        anchors = tail_result["anchors"]
        assert set(anchors.keys()) == {3, 4, 5}, f"Expected absolute indices {{3, 4, 5}}, got {set(anchors.keys())}"
        # The anchor entry for "line5" should record line_index=5
        tok_line5 = anchors[5]
        entry = amap._entries[tok_line5]
        assert entry.line_index == 5, f"Anchor for 'line5' has line_index={entry.line_index}; expected 5"
        assert entry.line_content == "line5"

    @pytest.mark.asyncio
    async def test_edit_via_tail_anchor_targets_correct_line(self, env):
        """An edit using a tail anchor modifies the correct line in the file."""
        vfs_obj, session, ns, agent, amap, fs_ops = env

        lines = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta"]
        content = "\n".join(lines)
        await vfs_obj.write(ns.id, "/edit_tail.txt", content.encode(), principal_id=agent.id)

        # Tail the last 2 lines ("epsilon", "zeta") — absolute indices 4, 5
        tail_result = await fs_ops.tail("/edit_tail.txt", 2)
        assert tail_result["error"] is None
        anchors = tail_result["anchors"]

        # Edit "zeta" (absolute index 5) with a replacement
        tok_zeta = anchors[5]
        edit_result = await fs_ops.edit("/edit_tail.txt", tok_zeta, tok_zeta, ["REPLACED_ZETA"])

        assert "version_number" in edit_result
        # Verify the edit modified only "zeta", not "alpha"
        raw = await vfs_obj.read(ns.id, "/edit_tail.txt", principal_id=agent.id)
        new_text = raw.decode()
        assert "REPLACED_ZETA" in new_text
        assert "alpha" in new_text  # untouched
        assert "zeta" not in new_text  # replaced


# ---------------------------------------------------------------------------
# Fix 5 regression: reconcile must fully replace path's anchor state
# ---------------------------------------------------------------------------


class TestReconcileDropsOrphanTokens:
    """Regression: reconcile must remove ALL tokens for a path, including duplicates.

    When allocate() is called multiple times for the same path (e.g. tail then cat),
    both token sets live in _entries simultaneously.  Before the fix, the atomic
    removal step only removed tokens reachable via the line_index→token reverse dict,
    leaving the earlier (duplicate-index) tokens as orphans.  After a reconcile, all
    such tokens should be gone — validate on any of them should raise AnchorConflictError.
    """

    def test_reconcile_drops_duplicate_allocation_orphans(self):
        """tail then cat: reconcile must remove all tokens for the path, not just one-per-index."""
        amap = AnchorMap()
        lines = ["line0", "line1", "line2"]

        # First allocation (simulates tail): allocates tokens for lines 1–2 (absolute indices 1, 2)
        tail_anchors = amap.allocate("/f.txt", 1, ["line1", "line2"], start_index=1)
        tail_tok_1 = tail_anchors[1]  # token for line index 1 from tail
        tail_tok_2 = tail_anchors[2]  # token for line index 2 from tail

        # Second allocation (simulates cat): allocates new tokens for ALL lines (indices 0, 1, 2).
        # The return value is intentionally discarded; we only need the side effect on _entries.
        amap.allocate("/f.txt", 1, lines, start_index=0)
        # Both tail tokens and cat tokens coexist in _entries before reconcile
        assert tail_tok_1 in amap._entries
        assert tail_tok_2 in amap._entries

        # Reconcile with a simple replacement (line 2 → "new_line")
        new_lines = ["line0", "line1", "new_line"]
        amap.reconcile("/f.txt", lines, new_lines, version_number=2)

        # After reconcile, ALL prior tokens for the path must be gone
        assert tail_tok_1 not in amap._entries, "orphan tail token at index 1 must be removed by reconcile"
        assert tail_tok_2 not in amap._entries, "orphan tail token at index 2 must be removed by reconcile"

        # Validate on the orphan tokens must raise AnchorConflictError
        with pytest.raises(AnchorConflictError):
            amap.validate(tail_tok_1, "/f.txt")
        with pytest.raises(AnchorConflictError):
            amap.validate(tail_tok_2, "/f.txt")

    def test_reconcile_entry_count_equals_new_line_count(self):
        """After reconcile, the number of entries for the path equals len(new_lines)."""
        amap = AnchorMap()
        lines = ["a", "b", "c", "d"]

        # Allocate twice (simulates tail then cat overlap)
        amap.allocate("/g.txt", 1, ["c", "d"], start_index=2)  # tail: 2 tokens
        amap.allocate("/g.txt", 1, lines, start_index=0)  # cat: 4 tokens (re-covers c, d)
        # 6 tokens in _entries for /g.txt before reconcile

        # Reconcile replaces all 4 lines with 3
        new_lines = ["a", "b", "X"]
        amap.reconcile("/g.txt", lines, new_lines, version_number=2)

        # Count entries remaining for /g.txt — must equal len(new_lines) = 3
        remaining = [tok for tok, entry in amap._entries.items() if entry.path == "/g.txt"]
        assert len(remaining) == 3, f"expected 3 entries for /g.txt after reconcile, got {len(remaining)}: {remaining}"

    @pytest.mark.asyncio
    async def test_tail_then_cat_then_edit_no_stale_tokens(self, env):
        """Integration: tail then cat then edit leaves no stale (orphan) tokens in the map."""
        vfs_obj, session, ns, agent, amap, fs_ops = env

        lines = ["line0", "line1", "line2", "line3", "line4"]
        content = "\n".join(lines)
        await vfs_obj.write(ns.id, "/overlap.txt", content.encode(), principal_id=agent.id)

        # Step 1: tail (allocates tokens for lines 3, 4)
        tail_result = await fs_ops.tail("/overlap.txt", 2)
        assert tail_result["error"] is None
        tail_toks = list(tail_result["anchors"].values())

        # Step 2: cat (allocates new tokens for all 5 lines; tail tokens are orphan duplicates)
        cat_result = await fs_ops.cat("/overlap.txt")
        assert cat_result["error"] is None
        cat_anchors = cat_result["anchors"]  # {line_index: token}

        # Step 3: edit line 0 via a cat anchor
        start_tok = cat_anchors[0]
        edit_result = await fs_ops.edit("/overlap.txt", start_tok, start_tok, ["REPLACED"])

        # All tail tokens must now be gone from _entries
        for tok in tail_toks:
            assert tok not in amap._entries, (
                f"Stale tail token {tok!r} still present after reconcile — orphan not removed"
            )

        # Entry count for the path must equal new_lines count
        remaining = [tok for tok, entry in amap._entries.items() if entry.path == "/overlap.txt"]
        expected_count = len(edit_result["lines"])
        assert len(remaining) == expected_count, f"expected {expected_count} entries for path, got {len(remaining)}"
