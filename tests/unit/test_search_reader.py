"""Tests for the guarded ContentReader.

Task group: Guarded Content Reader
Covers: GuardedContentReader/ReadsEnumeratedVersionNotLatest,
        GuardedContentReader/BudgetCeilingEnforced,
        GuardedContentReader/OutOfScopePathRefused.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from vfs.errors import PermissionDeniedError, ReadBudgetExceededError
from vfs.protocols.search import SearchMetaEntry
from vfs.search.reader import ContentReader

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _MockBlob:
    """In-memory blob store keyed by content_hash.

    KeyError on missing hash surfaces as a test failure (tests must wire blobs correctly).
    """

    def __init__(self, data: dict[str, bytes]) -> None:
        self._data = data

    async def get(self, ch: str) -> bytes:
        return self._data[ch]


def _entry(path: str, content_hash: str, *, version_id: str | None = None) -> SearchMetaEntry:
    return SearchMetaEntry(
        version_id=version_id or f"ver-{path}",
        path=path,
        content_hash=content_hash,
        size=0,
        updated_at=_now(),
    )


# ---------------------------------------------------------------------------
# GuardedContentReader/ReadsEnumeratedVersionNotLatest
# ---------------------------------------------------------------------------


class TestReadsEnumeratedVersionNotLatest:
    @pytest.mark.asyncio
    async def test_returns_enumerated_version_content(self):
        """ReadsEnumeratedVersionNotLatest: reader fetches by enumerated content_hash, not by path.

        Setup: entry enumerated with 'old-hash'; a newer 'new-hash' also exists in the blob
        store (simulating a concurrent write).  The reader must return 'old content'.
        """
        blob = _MockBlob(
            {
                "old-hash": b"old content",
                "new-hash": b"new content",
            }
        )
        entry = _entry("/data/file.txt", content_hash="old-hash")
        reader = ContentReader(entries=[entry], blob=blob, max_reads=5)

        result = await reader.read("/data/file.txt")

        assert result == b"old content", "reader must return the enumerated version, not the latest blob"

    @pytest.mark.asyncio
    async def test_reads_done_increments_on_success(self):
        blob = _MockBlob({"h1": b"hello"})
        entry = _entry("/f.txt", "h1")
        reader = ContentReader(entries=[entry], blob=blob, max_reads=5)

        assert reader.reads_done == 0
        await reader.read("/f.txt")
        assert reader.reads_done == 1


# ---------------------------------------------------------------------------
# GuardedContentReader/BudgetCeilingEnforced
# ---------------------------------------------------------------------------


class TestBudgetCeilingEnforced:
    @pytest.mark.asyncio
    async def test_n_reads_succeed_n_plus_1_raises(self):
        """BudgetCeilingEnforced: N reads succeed; (N+1)th raises ReadBudgetExceededError."""
        n = 3
        entries = [_entry(f"/f{i}.txt", f"h{i}") for i in range(n + 1)]
        blobs = {f"h{i}": f"content-{i}".encode() for i in range(n + 1)}
        reader = ContentReader(entries=entries, blob=_MockBlob(blobs), max_reads=n)

        for i in range(n):
            await reader.read(f"/f{i}.txt")
        assert reader.reads_done == n

        with pytest.raises(ReadBudgetExceededError):
            await reader.read(f"/f{n}.txt")

    @pytest.mark.asyncio
    async def test_zero_budget_raises_immediately(self):
        entry = _entry("/a.txt", "h0")
        reader = ContentReader(entries=[entry], blob=_MockBlob({"h0": b"x"}), max_reads=0)
        with pytest.raises(ReadBudgetExceededError):
            await reader.read("/a.txt")

    @pytest.mark.asyncio
    async def test_reads_remaining_decrements(self):
        entries = [_entry(f"/f{i}.txt", f"h{i}") for i in range(3)]
        blobs = {f"h{i}": b"x" for i in range(3)}
        reader = ContentReader(entries=entries, blob=_MockBlob(blobs), max_reads=3)

        assert reader.reads_remaining == 3
        await reader.read("/f0.txt")
        assert reader.reads_remaining == 2
        await reader.read("/f1.txt")
        assert reader.reads_remaining == 1


# ---------------------------------------------------------------------------
# GuardedContentReader/OutOfScopePathRefused
# ---------------------------------------------------------------------------


class TestOutOfScopePathRefused:
    @pytest.mark.asyncio
    async def test_path_not_in_entries_raises(self):
        """OutOfScopePathRefused: path absent from the enumerated set raises PermissionDeniedError."""
        reader = ContentReader(entries=[], blob=_MockBlob({}), max_reads=10)
        with pytest.raises(PermissionDeniedError):
            await reader.read("/secret/data.txt")

    @pytest.mark.asyncio
    async def test_out_of_scope_refused_even_if_blob_exists(self):
        """A path with content in the blob store but absent from entries is refused."""
        blob = _MockBlob({"known-hash": b"secret"})
        # Entry not registered → path is outside the search scope.
        reader = ContentReader(entries=[], blob=blob, max_reads=10)
        with pytest.raises(PermissionDeniedError):
            await reader.read("/known/path.txt")

    @pytest.mark.asyncio
    async def test_in_scope_path_succeeds(self):
        """A path present in entries is allowed and returns its content."""
        entry = _entry("/allowed.txt", "h-allow")
        reader = ContentReader(entries=[entry], blob=_MockBlob({"h-allow": b"ok"}), max_reads=5)
        result = await reader.read("/allowed.txt")
        assert result == b"ok"
