"""Tests for DefaultSearchProvider."""

from __future__ import annotations

from datetime import datetime, timezone

from hypothesis import given, settings, strategies as st
import pytest

from vfs.models import FileMeta, SearchResult, SearchType
from vfs.protocols.search import SearchProvider
from vfs.search.default import DefaultSearchProvider


@pytest.fixture
def provider():
    return DefaultSearchProvider()


def _file_meta(path: str) -> FileMeta:
    now = datetime.now(timezone.utc)
    return FileMeta(
        namespace_id="ns1",
        path=path,
        current_version_id="v1",
        current_version_number=1,
        created_at=now,
        updated_at=now,
    )


def _make_fetcher(store: dict[str, bytes]):
    """Build a fetch_content callback backed by an in-memory dict."""

    async def _fetch(path: str) -> bytes:
        return store[path]

    return _fetch


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
    async def test_index_returns_empty_dict(self, provider):
        result = await provider.index("/a.py", b"content", _file_meta("/a.py"))
        assert result == {}


# ---------------------------------------------------------------------------
# Glob search
# ---------------------------------------------------------------------------


class TestGlobSearch:
    @pytest.mark.asyncio
    async def test_non_recursive(self, provider):
        candidates = [_file_meta("/src/a.py"), _file_meta("/src/b.txt"), _file_meta("/src/sub/c.py")]
        results = await provider.search("*.py", "/src/", SearchType.GLOB, candidates)
        assert {r.path for r in results} == {"/src/a.py"}

    @pytest.mark.asyncio
    async def test_recursive_star_star(self, provider):
        candidates = [_file_meta("/src/a.py"), _file_meta("/src/b.txt"), _file_meta("/src/sub/c.py")]
        results = await provider.search("**/*.py", "/src/", SearchType.GLOB, candidates)
        assert {r.path for r in results} == {"/src/a.py", "/src/sub/c.py"}

    @pytest.mark.asyncio
    async def test_recursive_deeply_nested(self, provider):
        candidates = [
            _file_meta("/src/a.py"),
            _file_meta("/src/pkg/b.py"),
            _file_meta("/src/pkg/sub/deep/c.py"),
        ]
        results = await provider.search("**/*.py", "/src/", SearchType.GLOB, candidates)
        assert {r.path for r in results} == {"/src/a.py", "/src/pkg/b.py", "/src/pkg/sub/deep/c.py"}

    @pytest.mark.asyncio
    async def test_directory_qualified_recursive(self, provider):
        """pkg/**/*.py must only match .py files under pkg/, not sibling dirs."""
        candidates = [
            _file_meta("/src/a.py"),
            _file_meta("/src/pkg/b.py"),
            _file_meta("/src/pkg/sub/c.py"),
            _file_meta("/src/other/d.py"),
        ]
        results = await provider.search("pkg/**/*.py", "/src/", SearchType.GLOB, candidates)
        assert {r.path for r in results} == {"/src/pkg/b.py", "/src/pkg/sub/c.py"}

    @pytest.mark.asyncio
    async def test_non_recursive_excludes_nested(self, provider):
        """*.txt must not match files in subdirectories."""
        candidates = [_file_meta("/a.txt"), _file_meta("/sub/b.txt")]
        results = await provider.search("*.txt", "/", SearchType.GLOB, candidates)
        assert {r.path for r in results} == {"/a.txt"}

    @pytest.mark.asyncio
    async def test_scope_filtering(self, provider):
        """Glob only matches within the given scope."""
        candidates = [_file_meta("/src/a.py"), _file_meta("/other/b.py")]
        results = await provider.search("*.py", "/src/", SearchType.GLOB, candidates)
        assert {r.path for r in results} == {"/src/a.py"}

    @pytest.mark.asyncio
    async def test_empty_candidates(self, provider):
        results = await provider.search("*.py", "/", SearchType.GLOB, [])
        assert results == []

    @pytest.mark.asyncio
    async def test_none_candidates(self, provider):
        results = await provider.search("*.py", "/", SearchType.GLOB, None)
        assert results == []


# ---------------------------------------------------------------------------
# Find search
# ---------------------------------------------------------------------------


class TestFindSearch:
    @pytest.mark.asyncio
    async def test_find_by_name(self, provider):
        candidates = [_file_meta("/a.py"), _file_meta("/b.txt"), _file_meta("/sub/c.py")]
        results = await provider.search("*.py", "/", SearchType.FIND, candidates)
        assert {r.path for r in results} == {"/a.py", "/sub/c.py"}

    @pytest.mark.asyncio
    async def test_find_respects_scope(self, provider):
        candidates = [
            _file_meta("/src/a.py"),
            _file_meta("/other/b.py"),
            _file_meta("/src/sub/c.py"),
        ]
        results = await provider.search("*.py", "/src/", SearchType.FIND, candidates)
        assert {r.path for r in results} == {"/src/a.py", "/src/sub/c.py"}

    @pytest.mark.asyncio
    async def test_find_matches_across_depths(self, provider):
        """Find searches all depths within scope (unlike non-recursive glob)."""
        candidates = [_file_meta("/a.txt"), _file_meta("/d1/b.txt"), _file_meta("/d1/d2/c.txt")]
        results = await provider.search("*.txt", "/", SearchType.FIND, candidates)
        assert {r.path for r in results} == {"/a.txt", "/d1/b.txt", "/d1/d2/c.txt"}


# ---------------------------------------------------------------------------
# Regex search
# ---------------------------------------------------------------------------


class TestRegexSearch:
    @pytest.mark.asyncio
    async def test_single_match(self, provider):
        """Exactly one match returned with correct line number and context."""
        candidates = [_file_meta("/src/main.py")]
        fetcher = _make_fetcher({"/src/main.py": b"line 1\nline 2\n# TODO: fix this\nline 4\n"})
        results = await provider.search("TODO", "/src/", SearchType.REGEX, candidates, fetcher)
        assert len(results) == 1
        assert results[0] == SearchResult(
            path="/src/main.py",
            line_number=3,
            match_context="# TODO: fix this",
        )

    @pytest.mark.asyncio
    async def test_multiple_matches_same_file(self, provider):
        content = b"TODO first\nclean\nTODO second\n"
        candidates = [_file_meta("/a.py")]
        fetcher = _make_fetcher({"/a.py": content})
        results = await provider.search("TODO", "/", SearchType.REGEX, candidates, fetcher)
        assert len(results) == 2
        assert results[0].line_number == 1
        assert results[1].line_number == 3

    @pytest.mark.asyncio
    async def test_multiple_files(self, provider):
        candidates = [_file_meta("/a.py"), _file_meta("/b.py")]
        fetcher = _make_fetcher({"/a.py": b"match HERE\n", "/b.py": b"nothing\n"})
        results = await provider.search("HERE", "/", SearchType.REGEX, candidates, fetcher)
        assert len(results) == 1
        assert results[0].path == "/a.py"

    @pytest.mark.asyncio
    async def test_no_match(self, provider):
        candidates = [_file_meta("/src/main.py")]
        fetcher = _make_fetcher({"/src/main.py": b"nothing here\n"})
        results = await provider.search("NOTFOUND", "/src/", SearchType.REGEX, candidates, fetcher)
        assert results == []

    @pytest.mark.asyncio
    async def test_scope_filtering(self, provider):
        """Regex only searches files within scope."""
        candidates = [_file_meta("/src/a.py"), _file_meta("/other/b.py")]
        fetcher = _make_fetcher({"/src/a.py": b"match\n", "/other/b.py": b"match\n"})
        results = await provider.search("match", "/src/", SearchType.REGEX, candidates, fetcher)
        assert len(results) == 1
        assert results[0].path == "/src/a.py"

    @pytest.mark.asyncio
    async def test_without_fetcher_returns_empty(self, provider):
        candidates = [_file_meta("/src/main.py")]
        results = await provider.search("TODO", "/src/", SearchType.REGEX, candidates)
        assert results == []

    @pytest.mark.asyncio
    async def test_empty_file(self, provider):
        candidates = [_file_meta("/a.py")]
        fetcher = _make_fetcher({"/a.py": b""})
        results = await provider.search("anything", "/", SearchType.REGEX, candidates, fetcher)
        assert results == []

    @pytest.mark.asyncio
    async def test_binary_content_handled(self, provider):
        """Non-UTF-8 bytes don't crash; replacement characters are used."""
        candidates = [_file_meta("/bin")]
        fetcher = _make_fetcher({"/bin": b"\x80\x81\xff\n"})
        results = await provider.search(r"\ufffd", "/", SearchType.REGEX, candidates, fetcher)
        # Should not raise; may or may not match depending on replacement
        assert isinstance(results, list)


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
        candidates = [_file_meta(p) for p in paths]
        results = await provider.search("*", "/", SearchType.GLOB, candidates)
        for r in results:
            relative = r.path[1:]  # strip leading /
            assert "/" not in relative, f"Non-recursive glob returned nested path {r.path}"

    @settings(max_examples=50)
    @given(paths=st.lists(_rel_path, min_size=0, max_size=10))
    @pytest.mark.asyncio
    async def test_recursive_is_superset_of_non_recursive(self, paths: list[str]):
        """**/* must return at least everything * returns for the same scope."""
        provider = DefaultSearchProvider()
        candidates = [_file_meta(p) for p in paths]
        non_rec = await provider.search("*", "/", SearchType.GLOB, candidates)
        rec = await provider.search("**/*", "/", SearchType.GLOB, candidates)
        non_rec_paths = {r.path for r in non_rec}
        rec_paths = {r.path for r in rec}
        assert non_rec_paths <= rec_paths, f"Non-recursive results not a subset: {non_rec_paths - rec_paths}"

    @settings(max_examples=50)
    @given(paths=st.lists(_rel_path, min_size=0, max_size=10))
    @pytest.mark.asyncio
    async def test_scope_containment(self, paths: list[str]):
        """All results must start with the scope prefix."""
        provider = DefaultSearchProvider()
        candidates = [_file_meta(p) for p in paths]
        scope = "/src/"
        results = await provider.search("**/*", scope, SearchType.GLOB, candidates)
        for r in results:
            assert r.path.startswith(scope), f"{r.path} is outside scope {scope}"


class TestFindProperties:
    @settings(max_examples=50)
    @given(paths=st.lists(_rel_path, min_size=0, max_size=10))
    @pytest.mark.asyncio
    async def test_scope_containment(self, paths: list[str]):
        """Find results must be within scope."""
        provider = DefaultSearchProvider()
        candidates = [_file_meta(p) for p in paths]
        scope = "/src/"
        results = await provider.search("*", scope, SearchType.FIND, candidates)
        for r in results:
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
        candidates = [_file_meta("/f.txt")]
        fetcher = _make_fetcher({"/f.txt": content})
        # Use a literal pattern that is safe for regex
        results = await provider.search("a", "/", SearchType.REGEX, candidates, fetcher)
        num_lines = len(content.decode("utf-8", errors="replace").splitlines())
        for r in results:
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
        candidates = [_file_meta("/f.txt")]
        fetcher = _make_fetcher({"/f.txt": content})
        results = await provider.search("abc", "/", SearchType.REGEX, candidates, fetcher)
        for r in results:
            assert r.match_context is not None, "regex result must have match_context"
            assert "abc" in r.match_context, f"match_context {r.match_context!r} doesn't contain 'abc'"
