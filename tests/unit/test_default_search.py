"""Tests for DefaultSearchProvider — Phase 2 SearchRequest API.

The Phase 1 positional-argument API (query, scope, search_type, candidates, fetcher) has
been replaced with :class:`~vfs.protocols.search.SearchRequest`.  All tests are adapted
to the new shapes; no assertions have been weakened.

Dropped tests from Phase 1:
- ``test_none_candidates``: ``None`` is not a valid ``list[SearchMetaEntry]``;
  ``test_empty_candidates`` covers the equivalent empty-list case.
- ``test_without_fetcher_returns_empty``: no-reader graceful-degradation no longer exists;
  replaced by ``test_zero_budget_raises`` which asserts the stricter new contract.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib

from hypothesis import given, settings, strategies as st
import pytest

from vfs.errors import ReadBudgetExceededError
from vfs.models import FileMeta, SearchResult, SearchType
from vfs.protocols.search import (
    SearchLimits,
    SearchMetaEntry,
    SearchProvider,
    SearchRequest,
)
from vfs.search.default import DefaultSearchProvider
from vfs.search.reader import ContentReader


@pytest.fixture
def provider():
    return DefaultSearchProvider()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _file_meta(path: str) -> FileMeta:
    """FileMeta for index() tests."""
    now = datetime.now(timezone.utc)
    return FileMeta(
        namespace_id="ns1",
        path=path,
        current_version_id="v1",
        current_version_number=1,
        created_at=now,
        updated_at=now,
    )


class _MockBlob:
    """In-memory blob store keyed by content_hash."""

    def __init__(self, data: dict[str, bytes]) -> None:
        self._data = data

    async def get(self, ch: str) -> bytes:
        return self._data.get(ch, b"")


def _entries_only(paths: list[str]) -> list[SearchMetaEntry]:
    """Build SearchMetaEntry list with no real content (for metadata-only glob/find tests)."""
    now = datetime.now(timezone.utc)
    return [SearchMetaEntry(version_id=f"v_{p}", path=p, content_hash="00", size=0, updated_at=now) for p in paths]


def _reader_for(
    path_content: dict[str, bytes],
    max_reads: int = 100,
) -> tuple[list[SearchMetaEntry], ContentReader]:
    """Build (entries, reader) from a path→bytes mapping.

    Each entry's content_hash is the SHA-256 of its content so the guarded
    reader resolves the path to the correct immutable blob.
    """
    now = datetime.now(timezone.utc)
    entries: list[SearchMetaEntry] = []
    blobs: dict[str, bytes] = {}
    for path, content in path_content.items():
        ch = hashlib.sha256(content).hexdigest()
        blobs[ch] = content
        entries.append(
            SearchMetaEntry(
                version_id=f"v_{path}",
                path=path,
                content_hash=ch,
                size=len(content),
                updated_at=now,
            )
        )
    reader = ContentReader(entries=entries, blob=_MockBlob(blobs), max_reads=max_reads)
    return entries, reader


def _noop_reader(entries: list[SearchMetaEntry]) -> ContentReader:
    """Reader that raises immediately on any read attempt (safe for metadata-only tests)."""
    return ContentReader(entries=entries, blob=_MockBlob({}), max_reads=0)


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestProtocol:
    def test_conforms_to_protocol(self, provider):
        assert isinstance(provider, SearchProvider)

    def test_capabilities(self, provider):
        assert provider.capabilities() == {SearchType.GLOB, SearchType.FIND, SearchType.REGEX}


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------


class TestIndex:
    @pytest.mark.asyncio
    async def test_index_returns_none(self, provider):
        """DefaultSearchProvider.index() returns None — it produces no search artifacts."""
        result = await provider.index("/a.py", b"content", _file_meta("/a.py"))
        assert result is None


# ---------------------------------------------------------------------------
# Glob search
# ---------------------------------------------------------------------------


class TestGlobSearch:
    @pytest.mark.asyncio
    async def test_non_recursive(self, provider):
        entries = _entries_only(["/src/a.py", "/src/b.txt", "/src/sub/c.py"])
        req = SearchRequest(
            query="*.py",
            scope="/src/",
            search_type=SearchType.GLOB,
            search_metas=entries,
            read_content=_noop_reader(entries),
        )
        resp = await provider.search(req)
        assert {r.path for r in resp.results} == {"/src/a.py"}

    @pytest.mark.asyncio
    async def test_recursive_star_star(self, provider):
        entries = _entries_only(["/src/a.py", "/src/b.txt", "/src/sub/c.py"])
        req = SearchRequest(
            query="**/*.py",
            scope="/src/",
            search_type=SearchType.GLOB,
            search_metas=entries,
            read_content=_noop_reader(entries),
        )
        resp = await provider.search(req)
        assert {r.path for r in resp.results} == {"/src/a.py", "/src/sub/c.py"}

    @pytest.mark.asyncio
    async def test_recursive_deeply_nested(self, provider):
        entries = _entries_only(["/src/a.py", "/src/pkg/b.py", "/src/pkg/sub/deep/c.py"])
        req = SearchRequest(
            query="**/*.py",
            scope="/src/",
            search_type=SearchType.GLOB,
            search_metas=entries,
            read_content=_noop_reader(entries),
        )
        resp = await provider.search(req)
        assert {r.path for r in resp.results} == {"/src/a.py", "/src/pkg/b.py", "/src/pkg/sub/deep/c.py"}

    @pytest.mark.asyncio
    async def test_directory_qualified_recursive(self, provider):
        """pkg/**/*.py must only match .py files under pkg/, not sibling dirs."""
        entries = _entries_only(["/src/a.py", "/src/pkg/b.py", "/src/pkg/sub/c.py", "/src/other/d.py"])
        req = SearchRequest(
            query="pkg/**/*.py",
            scope="/src/",
            search_type=SearchType.GLOB,
            search_metas=entries,
            read_content=_noop_reader(entries),
        )
        resp = await provider.search(req)
        assert {r.path for r in resp.results} == {"/src/pkg/b.py", "/src/pkg/sub/c.py"}

    @pytest.mark.asyncio
    async def test_non_recursive_excludes_nested(self, provider):
        """*.txt must not match files in subdirectories."""
        entries = _entries_only(["/a.txt", "/sub/b.txt"])
        req = SearchRequest(
            query="*.txt",
            scope="/",
            search_type=SearchType.GLOB,
            search_metas=entries,
            read_content=_noop_reader(entries),
        )
        resp = await provider.search(req)
        assert {r.path for r in resp.results} == {"/a.txt"}

    @pytest.mark.asyncio
    async def test_scope_filtering(self, provider):
        """Glob only matches within the given scope."""
        entries = _entries_only(["/src/a.py", "/other/b.py"])
        req = SearchRequest(
            query="*.py",
            scope="/src/",
            search_type=SearchType.GLOB,
            search_metas=entries,
            read_content=_noop_reader(entries),
        )
        resp = await provider.search(req)
        assert {r.path for r in resp.results} == {"/src/a.py"}

    @pytest.mark.asyncio
    async def test_empty_candidates(self, provider):
        req = SearchRequest(
            query="*.py",
            scope="/",
            search_type=SearchType.GLOB,
            search_metas=[],
            read_content=_noop_reader([]),
        )
        resp = await provider.search(req)
        assert resp.results == []


# ---------------------------------------------------------------------------
# Find search
# ---------------------------------------------------------------------------


class TestFindSearch:
    @pytest.mark.asyncio
    async def test_find_by_name(self, provider):
        entries = _entries_only(["/a.py", "/b.txt", "/sub/c.py"])
        req = SearchRequest(
            query="*.py",
            scope="/",
            search_type=SearchType.FIND,
            search_metas=entries,
            read_content=_noop_reader(entries),
        )
        resp = await provider.search(req)
        assert {r.path for r in resp.results} == {"/a.py", "/sub/c.py"}

    @pytest.mark.asyncio
    async def test_find_respects_scope(self, provider):
        entries = _entries_only(["/src/a.py", "/other/b.py", "/src/sub/c.py"])
        req = SearchRequest(
            query="*.py",
            scope="/src/",
            search_type=SearchType.FIND,
            search_metas=entries,
            read_content=_noop_reader(entries),
        )
        resp = await provider.search(req)
        assert {r.path for r in resp.results} == {"/src/a.py", "/src/sub/c.py"}

    @pytest.mark.asyncio
    async def test_find_matches_across_depths(self, provider):
        """Find searches all depths within scope (unlike non-recursive glob)."""
        entries = _entries_only(["/a.txt", "/d1/b.txt", "/d1/d2/c.txt"])
        req = SearchRequest(
            query="*.txt",
            scope="/",
            search_type=SearchType.FIND,
            search_metas=entries,
            read_content=_noop_reader(entries),
        )
        resp = await provider.search(req)
        assert {r.path for r in resp.results} == {"/a.txt", "/d1/b.txt", "/d1/d2/c.txt"}


# ---------------------------------------------------------------------------
# Regex search
# ---------------------------------------------------------------------------


class TestRegexSearch:
    @pytest.mark.asyncio
    async def test_single_match(self, provider):
        """Exactly one match returned with correct line number and context."""
        entries, reader = _reader_for({"/src/main.py": b"line 1\nline 2\n# TODO: fix this\nline 4\n"})
        req = SearchRequest(
            query="TODO",
            scope="/src/",
            search_type=SearchType.REGEX,
            search_metas=entries,
            read_content=reader,
        )
        resp = await provider.search(req)
        assert len(resp.results) == 1
        assert resp.results[0] == SearchResult(
            path="/src/main.py",
            line_number=3,
            match_context="# TODO: fix this",
        )

    @pytest.mark.asyncio
    async def test_multiple_matches_same_file(self, provider):
        entries, reader = _reader_for({"/a.py": b"TODO first\nclean\nTODO second\n"})
        req = SearchRequest(
            query="TODO",
            scope="/",
            search_type=SearchType.REGEX,
            search_metas=entries,
            read_content=reader,
        )
        resp = await provider.search(req)
        assert len(resp.results) == 2
        assert resp.results[0].line_number == 1
        assert resp.results[1].line_number == 3

    @pytest.mark.asyncio
    async def test_multiple_files(self, provider):
        entries, reader = _reader_for({"/a.py": b"match HERE\n", "/b.py": b"nothing\n"})
        req = SearchRequest(
            query="HERE",
            scope="/",
            search_type=SearchType.REGEX,
            search_metas=entries,
            read_content=reader,
        )
        resp = await provider.search(req)
        assert len(resp.results) == 1
        assert resp.results[0].path == "/a.py"

    @pytest.mark.asyncio
    async def test_no_match(self, provider):
        entries, reader = _reader_for({"/src/main.py": b"nothing here\n"})
        req = SearchRequest(
            query="NOTFOUND",
            scope="/src/",
            search_type=SearchType.REGEX,
            search_metas=entries,
            read_content=reader,
        )
        resp = await provider.search(req)
        assert resp.results == []

    @pytest.mark.asyncio
    async def test_scope_filtering(self, provider):
        """Regex only searches files within scope."""
        entries, reader = _reader_for({"/src/a.py": b"match\n", "/other/b.py": b"match\n"})
        req = SearchRequest(
            query="match",
            scope="/src/",
            search_type=SearchType.REGEX,
            search_metas=entries,
            read_content=reader,
        )
        resp = await provider.search(req)
        assert len(resp.results) == 1
        assert resp.results[0].path == "/src/a.py"

    @pytest.mark.asyncio
    async def test_zero_budget_raises(self, provider):
        """max_reads=0 raises ReadBudgetExceededError on the first in-scope file.

        Replaces Phase 1 ``test_without_fetcher_returns_empty``: the no-reader
        graceful-degradation path no longer exists; budget exhaustion fails loud.
        """
        entries, reader = _reader_for({"/src/main.py": b"TODO\n"}, max_reads=0)
        req = SearchRequest(
            query="TODO",
            scope="/src/",
            search_type=SearchType.REGEX,
            search_metas=entries,
            read_content=reader,
        )
        with pytest.raises(ReadBudgetExceededError):
            await provider.search(req)

    @pytest.mark.asyncio
    async def test_empty_file(self, provider):
        entries, reader = _reader_for({"/a.py": b""})
        req = SearchRequest(
            query="anything",
            scope="/",
            search_type=SearchType.REGEX,
            search_metas=entries,
            read_content=reader,
        )
        resp = await provider.search(req)
        assert resp.results == []

    @pytest.mark.asyncio
    async def test_binary_content_handled(self, provider):
        """Non-UTF-8 bytes don't crash; replacement characters are used."""
        entries, reader = _reader_for({"/bin": b"\x80\x81\xff\n"})
        req = SearchRequest(
            query=r"�",
            scope="/",
            search_type=SearchType.REGEX,
            search_metas=entries,
            read_content=reader,
        )
        resp = await provider.search(req)
        assert isinstance(resp.results, list)


# ---------------------------------------------------------------------------
# Hypothesis property tests
# ---------------------------------------------------------------------------

# Strategy: generate valid POSIX path segments (no slashes, no empty, no null)
_segment = st.text(
    alphabet=st.sampled_from("abcdefghijklmnopqrstuvwxyz0123456789_-."),
    min_size=1,
    max_size=12,
)
# Strategy: generate a path with 1-4 segments
_rel_path = st.lists(_segment, min_size=1, max_size=4).map(lambda parts: "/" + "/".join(parts))


class TestGlobProperties:
    @settings(max_examples=50)
    @given(paths=st.lists(_rel_path, min_size=0, max_size=10))
    @pytest.mark.asyncio
    async def test_non_recursive_never_returns_nested(self, paths: list[str]):
        """Non-recursive glob *.X must never return files with / in the relative path."""
        provider = DefaultSearchProvider()
        entries = _entries_only(paths)
        req = SearchRequest(
            query="*",
            scope="/",
            search_type=SearchType.GLOB,
            search_metas=entries,
            read_content=_noop_reader(entries),
        )
        resp = await provider.search(req)
        for r in resp.results:
            relative = r.path[1:]  # strip leading /
            assert "/" not in relative, f"Non-recursive glob returned nested path {r.path}"

    @settings(max_examples=50)
    @given(paths=st.lists(_rel_path, min_size=0, max_size=10))
    @pytest.mark.asyncio
    async def test_recursive_is_superset_of_non_recursive(self, paths: list[str]):
        """**/* must return at least everything * returns for the same scope."""
        provider = DefaultSearchProvider()
        entries = _entries_only(paths)
        reader = _noop_reader(entries)
        non_rec_req = SearchRequest(
            query="*",
            scope="/",
            search_type=SearchType.GLOB,
            search_metas=entries,
            read_content=reader,
        )
        rec_req = SearchRequest(
            query="**/*",
            scope="/",
            search_type=SearchType.GLOB,
            search_metas=entries,
            read_content=reader,
        )
        non_rec = await provider.search(non_rec_req)
        rec = await provider.search(rec_req)
        non_rec_paths = {r.path for r in non_rec.results}
        rec_paths = {r.path for r in rec.results}
        assert non_rec_paths <= rec_paths, f"Non-recursive results not a subset: {non_rec_paths - rec_paths}"

    @settings(max_examples=50)
    @given(paths=st.lists(_rel_path, min_size=0, max_size=10))
    @pytest.mark.asyncio
    async def test_scope_containment(self, paths: list[str]):
        """All results must start with the scope prefix."""
        provider = DefaultSearchProvider()
        entries = _entries_only(paths)
        scope = "/src/"
        req = SearchRequest(
            query="**/*",
            scope=scope,
            search_type=SearchType.GLOB,
            search_metas=entries,
            read_content=_noop_reader(entries),
        )
        resp = await provider.search(req)
        for r in resp.results:
            assert r.path.startswith(scope), f"{r.path} is outside scope {scope}"


class TestFindProperties:
    @settings(max_examples=50)
    @given(paths=st.lists(_rel_path, min_size=0, max_size=10))
    @pytest.mark.asyncio
    async def test_scope_containment(self, paths: list[str]):
        """Find results must be within scope."""
        provider = DefaultSearchProvider()
        entries = _entries_only(paths)
        scope = "/src/"
        req = SearchRequest(
            query="*",
            scope=scope,
            search_type=SearchType.FIND,
            search_metas=entries,
            read_content=_noop_reader(entries),
        )
        resp = await provider.search(req)
        for r in resp.results:
            assert r.path.startswith(scope), f"{r.path} is outside scope {scope}"


class TestRegexProperties:
    @settings(max_examples=30)
    @given(
        lines=st.lists(
            st.text(alphabet=st.characters(categories=("L", "N", "P", "Z")), min_size=0, max_size=40),
            min_size=1,
            max_size=20,
        ),
    )
    @pytest.mark.asyncio
    async def test_line_numbers_are_valid(self, lines: list[str]):
        """Every returned line_number must be in [1, num_lines]."""
        provider = DefaultSearchProvider()
        content = "\n".join(lines).encode()
        entries, reader = _reader_for({"/f.txt": content})
        req = SearchRequest(
            query="a",
            scope="/",
            search_type=SearchType.REGEX,
            search_metas=entries,
            read_content=reader,
        )
        resp = await provider.search(req)
        num_lines = len(content.decode("utf-8", errors="replace").splitlines())
        for r in resp.results:
            assert r.line_number is not None, "regex result must have line_number"
            assert 1 <= r.line_number <= num_lines, f"line_number {r.line_number} out of range [1, {num_lines}]"

    @settings(max_examples=30)
    @given(
        lines=st.lists(
            st.text(alphabet=st.characters(categories=("L", "N", "P", "Z")), min_size=0, max_size=40),
            min_size=1,
            max_size=20,
        ),
    )
    @pytest.mark.asyncio
    async def test_match_context_contains_pattern(self, lines: list[str]):
        """Every match_context must actually contain the searched literal."""
        provider = DefaultSearchProvider()
        content = "\n".join(lines).encode()
        entries, reader = _reader_for({"/f.txt": content})
        req = SearchRequest(
            query="abc",
            scope="/",
            search_type=SearchType.REGEX,
            search_metas=entries,
            read_content=reader,
        )
        resp = await provider.search(req)
        for r in resp.results:
            assert r.match_context is not None, "regex result must have match_context"
            assert "abc" in r.match_context, f"match_context {r.match_context!r} doesn't contain 'abc'"
