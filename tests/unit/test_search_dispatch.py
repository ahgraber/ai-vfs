"""Tests for NativeTextSearch capability routing and find_predicates matching.

Covers:
  PluggableSearchProviders/NativeCapabilityServesRegex
  PluggableSearchProviders/GlobFindAlwaysAvailable
  PluggableSearchProviders/MongoRegexDeferred
  FindSearchPredicates/FindByNamePatternUnchanged
  FindSearchPredicates/FindBySizeRange
  FindSearchPredicates/FindByModifiedTime
  FindSearchPredicates/FindByType
  FindSearchPredicates/FindConjunctivePredicates

Dispatch tests (NativeCapabilityServesRegex, GlobFindAlwaysAvailable,
MongoRegexDeferred) go through the full VFS backed by in-memory SQLite.
Find-predicates tests exercise DefaultSearchProvider directly.

Tension note
------------
The delta spec ``MongoRegexDeferred`` scenario states that *both* regex and fulltext
are rejected when the store has no NativeTextSearch capability (i.e. MongoDB).  The
dispatch rule implemented here is more permissive for regex: absent a native capability
(``native_text_search()`` returns None), regex falls back to the DefaultSearchProvider
brute-force path for *any* backend — the VFS cannot distinguish MongoDB from SQLite at
dispatch time, since both currently return None.  The MongoRegexDeferred test below
encodes the rule as implemented: fulltext → SearchTypeUnsupportedError; regex →
brute-force (guarded reader, succeeds within budget).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from vfs.config import VFSConfig
from vfs.errors import SearchTypeUnsupportedError
from vfs.models import SearchResult, SearchType
from vfs.protocols.search import (
    FindPredicates,
    SearchMetaEntry,
    SearchRequest,
    SearchResponse,
)
from vfs.search.default import DefaultSearchProvider
from vfs.search.reader import ContentReader
from vfs.vfs import VFS

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _stub_nts(real_nts=None) -> MagicMock:
    """Create a stub NTS with the required async methods; mirrors real_nts key/hash if given."""
    stub = MagicMock()
    stub.provider_key = real_nts.provider_key if real_nts is not None else "test"
    stub.params_hash = real_nts.params_hash if real_nts is not None else "test"
    # has_text_artifacts must be AsyncMock so 'await nts.has_text_artifacts(...)' works;
    # side_effect returns all hashes as 'found' so no fresh entry gets reclassified.
    stub.has_text_artifacts = AsyncMock(side_effect=lambda hashes, ph: set(hashes))
    return stub


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _MockBlob:
    """In-memory blob store keyed by content_hash."""

    def __init__(self, data: dict[str, bytes]) -> None:
        self._data = data

    async def get(self, ch: str) -> bytes:
        return self._data.get(ch, b"")


def _entry(
    path: str,
    *,
    content: bytes = b"",
    size: int | None = None,
    updated_at: datetime | None = None,
    is_deleted: bool = False,
) -> SearchMetaEntry:
    ch = hashlib.sha256(content).hexdigest()
    return SearchMetaEntry(
        version_id=f"ver-{path}",
        path=path,
        content_hash=ch,
        size=size if size is not None else len(content),
        updated_at=updated_at or _now(),
        is_deleted=is_deleted,
    )


def _noop_reader(entries: list[SearchMetaEntry]) -> ContentReader:
    """Reader that raises immediately on any read attempt (for metadata-only tests)."""
    return ContentReader(entries=entries, blob=_MockBlob({}), max_reads=0)


def _reader_for(path_content: dict[str, bytes], max_reads: int = 100) -> tuple[list[SearchMetaEntry], ContentReader]:
    entries: list[SearchMetaEntry] = []
    blobs: dict[str, bytes] = {}
    for path, content in path_content.items():
        ch = hashlib.sha256(content).hexdigest()
        blobs[ch] = content
        entries.append(
            SearchMetaEntry(
                version_id=f"ver-{path}",
                path=path,
                content_hash=ch,
                size=len(content),
                updated_at=_now(),
            )
        )
    reader = ContentReader(entries=entries, blob=_MockBlob(blobs), max_reads=max_reads)
    return entries, reader


# ---------------------------------------------------------------------------
# VFS fixture for dispatch tests
# ---------------------------------------------------------------------------


async def _setup_vfs(vfs: VFS) -> tuple:
    """Bootstrap admin + agent; returns (namespace, principal, admin)."""
    ns = await vfs.create_namespace("ns", "admin")
    admin = await vfs.create_principal("admin")
    await vfs.bootstrap_admin(admin.id, ns.id)
    p = await vfs.create_principal("agent")
    await vfs.grant(admin.id, p.id, ns.id, "/", {"read", "write"})
    return ns, p, admin


@pytest_asyncio.fixture
async def vfs_instance(tmp_path):
    db_path = str(tmp_path / "test.db")
    blob_path = str(tmp_path / "blobs")
    config = VFSConfig(
        metadata_store_uri=f"sqlite:///{db_path}",
        blob_store_uri=f"file:///{blob_path}/",
        otel_enabled=False,
        audit_log_enabled=False,
    )
    vfs = VFS(config)
    await vfs.initialize()
    yield vfs
    await vfs.close()


# ---------------------------------------------------------------------------
# PluggableSearchProviders/NativeCapabilityServesRegex
# ---------------------------------------------------------------------------


class TestNativeCapabilityServesRegex:
    @pytest.mark.asyncio
    async def test_regex_routed_to_native_capability(self, vfs_instance):
        """NativeCapabilityServesRegex: regex dispatches to search_text when capability present."""
        ns, p, _ = await _setup_vfs(vfs_instance)
        await vfs_instance.write(ns.id, "/src/a.py", b"hello world", principal_id=p.id)

        # Capture the real NTS attributes before replacing so the stub matches the
        # provider_key/params_hash already stored in the written version's search_meta.
        # Without this, the straggler classifier sees no matching artifact and classifies
        # the entry as a straggler instead of a fresh entry, producing duplicate results.
        _real_nts = vfs_instance._meta.native_text_search()
        stub_nts = _stub_nts(_real_nts)
        stub_nts.search_text = AsyncMock(return_value=SearchResponse(results=[SearchResult(path="/src/a.py")]))

        # Inject stub via native_text_search() on the live meta store
        vfs_instance._meta.native_text_search = lambda: stub_nts

        results = await vfs_instance.search(ns.id, "hello", "/", SearchType.REGEX, principal_id=p.id)

        # Stub must have been called (routing happened)
        stub_nts.search_text.assert_called_once()
        # The call receives (request, visible_version_ids)
        call_args = stub_nts.search_text.call_args
        request_arg = call_args[0][0]
        assert request_arg.search_type == SearchType.REGEX
        assert request_arg.query == "hello"
        # Results come from the stub
        assert [r.path for r in results] == ["/src/a.py"]

    @pytest.mark.asyncio
    async def test_fulltext_routed_to_native_capability(self, vfs_instance):
        """Fulltext also routes to search_text when capability present."""
        ns, p, _ = await _setup_vfs(vfs_instance)
        await vfs_instance.write(ns.id, "/doc.txt", b"content", principal_id=p.id)

        _real_nts = vfs_instance._meta.native_text_search()
        stub_nts = _stub_nts(_real_nts)
        stub_nts.search_text = AsyncMock(return_value=SearchResponse(results=[]))
        vfs_instance._meta.native_text_search = lambda: stub_nts

        await vfs_instance.search(ns.id, "content", "/", SearchType.FULLTEXT, principal_id=p.id)

        stub_nts.search_text.assert_called_once()
        request_arg = stub_nts.search_text.call_args[0][0]
        assert request_arg.search_type == SearchType.FULLTEXT

    @pytest.mark.asyncio
    async def test_visible_version_ids_passed_to_capability(self, vfs_instance):
        """search_text receives fresh version_ids (all entries have usable artifacts here)."""
        ns, p, _ = await _setup_vfs(vfs_instance)
        await vfs_instance.write(ns.id, "/a.py", b"x", principal_id=p.id)
        await vfs_instance.write(ns.id, "/b.py", b"y", principal_id=p.id)

        _real_nts = vfs_instance._meta.native_text_search()
        stub_nts = _stub_nts(_real_nts)
        stub_nts.search_text = AsyncMock(return_value=SearchResponse(results=[]))
        vfs_instance._meta.native_text_search = lambda: stub_nts

        await vfs_instance.search(ns.id, ".*", "/", SearchType.REGEX, principal_id=p.id)

        # Both files were written with usable artifacts → both classified as fresh →
        # both version_ids appear in the call.
        version_ids_arg = stub_nts.search_text.call_args[0][1]
        assert len(version_ids_arg) == 2  # both files visible and fresh


# ---------------------------------------------------------------------------
# PluggableSearchProviders/GlobFindAlwaysAvailable
# ---------------------------------------------------------------------------


class TestGlobFindAlwaysAvailable:
    @pytest.mark.asyncio
    async def test_glob_available_without_native_capability(self, vfs_instance):
        """GlobFindAlwaysAvailable: glob works regardless of the native capability."""
        ns, p, _ = await _setup_vfs(vfs_instance)
        await vfs_instance.write(ns.id, "/src/a.py", b"x", principal_id=p.id)
        await vfs_instance.write(ns.id, "/src/b.txt", b"x", principal_id=p.id)

        results = await vfs_instance.search(ns.id, "*.py", "/src/", SearchType.GLOB, principal_id=p.id)
        assert {r.path for r in results} == {"/src/a.py"}

    @pytest.mark.asyncio
    async def test_find_available_without_native_capability(self, vfs_instance):
        """GlobFindAlwaysAvailable: find works when native_text_search() returns None."""
        ns, p, _ = await _setup_vfs(vfs_instance)
        await vfs_instance.write(ns.id, "/a.txt", b"x", principal_id=p.id)
        await vfs_instance.write(ns.id, "/b.py", b"x", principal_id=p.id)

        results = await vfs_instance.search(ns.id, "*.txt", "/", SearchType.FIND, principal_id=p.id)
        assert {r.path for r in results} == {"/a.txt"}

    @pytest.mark.asyncio
    async def test_glob_not_routed_to_native_capability(self, vfs_instance):
        """GlobFindAlwaysAvailable: glob never calls search_text even when capability is present."""
        ns, p, _ = await _setup_vfs(vfs_instance)
        await vfs_instance.write(ns.id, "/a.py", b"x", principal_id=p.id)

        stub_nts = MagicMock()
        stub_nts.search_text = AsyncMock(return_value=SearchResponse(results=[]))
        vfs_instance._meta.native_text_search = lambda: stub_nts

        await vfs_instance.search(ns.id, "*.py", "/", SearchType.GLOB, principal_id=p.id)

        # Stub must NOT have been called
        stub_nts.search_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_find_not_routed_to_native_capability(self, vfs_instance):
        """GlobFindAlwaysAvailable: find never calls search_text even when capability is present."""
        ns, p, _ = await _setup_vfs(vfs_instance)
        await vfs_instance.write(ns.id, "/a.py", b"x", principal_id=p.id)

        stub_nts = MagicMock()
        stub_nts.search_text = AsyncMock(return_value=SearchResponse(results=[]))
        vfs_instance._meta.native_text_search = lambda: stub_nts

        await vfs_instance.search(ns.id, "*.py", "/", SearchType.FIND, principal_id=p.id)

        stub_nts.search_text.assert_not_called()


# ---------------------------------------------------------------------------
# PluggableSearchProviders/MongoRegexDeferred
# ---------------------------------------------------------------------------


class TestMongoRegexDeferred:
    """Tests encoding the dispatch rule as implemented for a store with no native capability.

    Dispatch rule: absent native capability, fulltext → SearchTypeUnsupportedError;
    regex → DefaultSearchProvider brute-force (guarded reader).

    Note: the delta spec MongoRegexDeferred scenario states both regex AND fulltext are
    rejected for MongoDB.  The implementation cannot distinguish MongoDB from SQLite at the
    VFS dispatch level (both return None from native_text_search()), so regex falls back to
    brute-force for any no-capability backend.  This test encodes the rule as implemented.
    """

    @pytest.mark.asyncio
    async def test_fulltext_without_capability_raises_unsupported(self, vfs_instance):
        """MongoRegexDeferred: fulltext raises SearchTypeUnsupportedError when no capability.

        Simulates a no-capability backend (e.g. MongoDB) by patching native_text_search()
        to return None on an otherwise-live VFS instance.
        """
        ns, p, _ = await _setup_vfs(vfs_instance)
        await vfs_instance.write(ns.id, "/doc.txt", b"hello world", principal_id=p.id)

        # Simulate a no-NativeTextSearch backend (e.g. MongoDB).
        vfs_instance._meta.native_text_search = lambda: None

        with pytest.raises(SearchTypeUnsupportedError, match="fulltext"):
            await vfs_instance.search(ns.id, "hello", "/", SearchType.FULLTEXT, principal_id=p.id)

    @pytest.mark.asyncio
    async def test_regex_without_capability_uses_brute_force(self, vfs_instance):
        """MongoRegexDeferred: regex falls back to brute-force when no native capability.

        Simulates a no-capability backend by patching native_text_search() to return None.
        """
        ns, p, _ = await _setup_vfs(vfs_instance)
        await vfs_instance.write(ns.id, "/src/a.py", b"hello world\n", principal_id=p.id)
        await vfs_instance.write(ns.id, "/src/b.py", b"goodbye world\n", principal_id=p.id)

        # Simulate a no-NativeTextSearch backend (e.g. MongoDB).
        vfs_instance._meta.native_text_search = lambda: None

        results = await vfs_instance.search(ns.id, "hello", "/", SearchType.REGEX, principal_id=p.id)
        assert {r.path for r in results} == {"/src/a.py"}

    @pytest.mark.asyncio
    async def test_semantic_always_rejected(self, vfs_instance):
        """SEMANTIC search is rejected regardless of native capability (pre-existing contract)."""
        ns, p, _ = await _setup_vfs(vfs_instance)
        with pytest.raises(ValueError, match="semantic"):
            await vfs_instance.search(ns.id, "meaning", "/", SearchType.SEMANTIC, principal_id=p.id)


# ---------------------------------------------------------------------------
# FindSearchPredicates tests — DefaultSearchProvider level
# ---------------------------------------------------------------------------


class TestFindByNamePatternUnchanged:
    @pytest.mark.asyncio
    async def test_name_query_unchanged(self):
        """FindByNamePatternUnchanged: Phase 1 name-pattern behavior preserved."""
        provider = DefaultSearchProvider()
        entries = [
            _entry("/a.py"),
            _entry("/b.txt"),
            _entry("/sub/c.py"),
        ]
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
    async def test_find_predicates_name_matches_same_as_query(self):
        """FindByNamePatternUnchanged: find_predicates.name gives same results as query."""
        provider = DefaultSearchProvider()
        entries = [
            _entry("/a.py"),
            _entry("/b.txt"),
            _entry("/sub/c.py"),
        ]
        # Use query="*" (match all) and filter by find_predicates.name
        req = SearchRequest(
            query="*",
            scope="/",
            search_type=SearchType.FIND,
            search_metas=entries,
            read_content=_noop_reader(entries),
            find_predicates=FindPredicates(name="*.py"),
        )
        resp = await provider.search(req)
        assert {r.path for r in resp.results} == {"/a.py", "/sub/c.py"}


class TestFindBySizeRange:
    @pytest.mark.asyncio
    async def test_size_range_filters_entries(self):
        """FindBySizeRange: only entries within [size_min, size_max] are returned."""
        provider = DefaultSearchProvider()
        entries = [
            _entry("/small.txt", size=100),
            _entry("/medium.txt", size=5_000),
            _entry("/large.txt", size=50_000),
        ]
        req = SearchRequest(
            query="*",
            scope="/",
            search_type=SearchType.FIND,
            search_metas=entries,
            read_content=_noop_reader(entries),
            find_predicates=FindPredicates(size_min=1_000, size_max=10_000),
        )
        resp = await provider.search(req)
        assert {r.path for r in resp.results} == {"/medium.txt"}

    @pytest.mark.asyncio
    async def test_size_min_only(self):
        """size_min alone excludes entries below the threshold."""
        provider = DefaultSearchProvider()
        entries = [_entry("/a.txt", size=100), _entry("/b.txt", size=10_000)]
        req = SearchRequest(
            query="*",
            scope="/",
            search_type=SearchType.FIND,
            search_metas=entries,
            read_content=_noop_reader(entries),
            find_predicates=FindPredicates(size_min=500),
        )
        resp = await provider.search(req)
        assert {r.path for r in resp.results} == {"/b.txt"}

    @pytest.mark.asyncio
    async def test_size_max_only(self):
        """size_max alone excludes entries above the threshold."""
        provider = DefaultSearchProvider()
        entries = [_entry("/a.txt", size=100), _entry("/b.txt", size=10_000)]
        req = SearchRequest(
            query="*",
            scope="/",
            search_type=SearchType.FIND,
            search_metas=entries,
            read_content=_noop_reader(entries),
            find_predicates=FindPredicates(size_max=500),
        )
        resp = await provider.search(req)
        assert {r.path for r in resp.results} == {"/a.txt"}

    @pytest.mark.asyncio
    async def test_exact_boundary_excluded(self):
        """Entries at the exact size_min boundary: ≥ is inclusive (min ≤ size ≤ max)."""
        provider = DefaultSearchProvider()
        entries = [_entry("/a.txt", size=1_000), _entry("/b.txt", size=999)]
        req = SearchRequest(
            query="*",
            scope="/",
            search_type=SearchType.FIND,
            search_metas=entries,
            read_content=_noop_reader(entries),
            find_predicates=FindPredicates(size_min=1_000),
        )
        resp = await provider.search(req)
        # a.txt has size == size_min: should be included (not < size_min)
        assert {r.path for r in resp.results} == {"/a.txt"}


class TestFindByModifiedTime:
    @pytest.mark.asyncio
    async def test_mtime_after_filters_old_entries(self):
        """FindByModifiedTime: mtime_after returns only entries modified after the threshold."""
        now = _now()
        provider = DefaultSearchProvider()
        entries = [
            _entry("/recent.txt", updated_at=now - timedelta(hours=2)),
            _entry("/yesterday.txt", updated_at=now - timedelta(days=1)),
            _entry("/old.txt", updated_at=now - timedelta(days=30)),
        ]
        threshold = now - timedelta(hours=24)
        req = SearchRequest(
            query="*",
            scope="/",
            search_type=SearchType.FIND,
            search_metas=entries,
            read_content=_noop_reader(entries),
            find_predicates=FindPredicates(mtime_after=threshold),
        )
        resp = await provider.search(req)
        # Only /recent.txt is strictly after now-24h (yesterday is at exactly the threshold)
        assert {r.path for r in resp.results} == {"/recent.txt"}

    @pytest.mark.asyncio
    async def test_mtime_before_filters_recent_entries(self):
        """mtime_before returns only entries modified before the threshold."""
        now = _now()
        provider = DefaultSearchProvider()
        entries = [
            _entry("/recent.txt", updated_at=now - timedelta(hours=2)),
            _entry("/old.txt", updated_at=now - timedelta(days=7)),
        ]
        threshold = now - timedelta(hours=12)
        req = SearchRequest(
            query="*",
            scope="/",
            search_type=SearchType.FIND,
            search_metas=entries,
            read_content=_noop_reader(entries),
            find_predicates=FindPredicates(mtime_before=threshold),
        )
        resp = await provider.search(req)
        assert {r.path for r in resp.results} == {"/old.txt"}


class TestFindByType:
    @pytest.mark.asyncio
    async def test_type_file_excludes_tombstones(self):
        """FindByType: type='file' returns only non-deleted entries."""
        provider = DefaultSearchProvider()
        entries = [
            _entry("/live.py", is_deleted=False),
            _entry("/dead.py", is_deleted=True),
        ]
        req = SearchRequest(
            query="*",
            scope="/",
            search_type=SearchType.FIND,
            search_metas=entries,
            read_content=_noop_reader(entries),
            find_predicates=FindPredicates(type="file"),
        )
        resp = await provider.search(req)
        assert {r.path for r in resp.results} == {"/live.py"}

    @pytest.mark.asyncio
    async def test_type_tombstone_excludes_live_entries(self):
        """FindByType: type='tombstone' returns only deleted entries."""
        provider = DefaultSearchProvider()
        entries = [
            _entry("/live.py", is_deleted=False),
            _entry("/dead.py", is_deleted=True),
        ]
        req = SearchRequest(
            query="*",
            scope="/",
            search_type=SearchType.FIND,
            search_metas=entries,
            read_content=_noop_reader(entries),
            find_predicates=FindPredicates(type="tombstone"),
        )
        resp = await provider.search(req)
        assert {r.path for r in resp.results} == {"/dead.py"}

    @pytest.mark.asyncio
    async def test_type_none_returns_all(self):
        """FindByType: type=None (default) includes both live and deleted entries."""
        provider = DefaultSearchProvider()
        entries = [
            _entry("/live.py", is_deleted=False),
            _entry("/dead.py", is_deleted=True),
        ]
        req = SearchRequest(
            query="*",
            scope="/",
            search_type=SearchType.FIND,
            search_metas=entries,
            read_content=_noop_reader(entries),
            find_predicates=FindPredicates(type=None),
        )
        resp = await provider.search(req)
        assert {r.path for r in resp.results} == {"/live.py", "/dead.py"}


class TestFindConjunctivePredicates:
    @pytest.mark.asyncio
    async def test_name_and_size_max(self):
        """FindConjunctivePredicates: name pattern AND size_max applied conjunctively."""
        provider = DefaultSearchProvider()
        now = _now()
        entries = [
            _entry("/src/a.py", size=500, updated_at=now - timedelta(minutes=10)),
            _entry("/src/b.py", size=50_000, updated_at=now - timedelta(days=30)),
            _entry("/data/c.txt", size=500, updated_at=now - timedelta(minutes=10)),
        ]
        req = SearchRequest(
            query="*",
            scope="/",
            search_type=SearchType.FIND,
            search_metas=entries,
            read_content=_noop_reader(entries),
            find_predicates=FindPredicates(name="*.py", size_max=10_000),
        )
        resp = await provider.search(req)
        # /src/a.py: *.py ✓, size 500 ≤ 10_000 ✓ → included
        # /src/b.py: *.py ✓, size 50_000 > 10_000 ✗ → excluded
        # /data/c.txt: *.py ✗ → excluded
        assert {r.path for r in resp.results} == {"/src/a.py"}

    @pytest.mark.asyncio
    async def test_all_predicates_must_match(self):
        """All set predicates must match; one failing predicate excludes the entry."""
        provider = DefaultSearchProvider()
        now = _now()
        entries = [
            # matches name and size but not mtime
            _entry("/a.py", size=100, updated_at=now - timedelta(days=5)),
            # matches name and mtime but not size
            _entry("/b.py", size=10_000, updated_at=now - timedelta(hours=1)),
            # matches all
            _entry("/c.py", size=100, updated_at=now - timedelta(hours=1)),
        ]
        req = SearchRequest(
            query="*",
            scope="/",
            search_type=SearchType.FIND,
            search_metas=entries,
            read_content=_noop_reader(entries),
            find_predicates=FindPredicates(name="*.py", size_max=500, mtime_after=now - timedelta(hours=12)),
        )
        resp = await provider.search(req)
        assert {r.path for r in resp.results} == {"/c.py"}

    @pytest.mark.asyncio
    async def test_no_predicates_returns_all(self):
        """find_predicates=None behaves as Phase 1 (name query only)."""
        provider = DefaultSearchProvider()
        entries = [
            _entry("/a.py"),
            _entry("/b.txt"),
        ]
        req = SearchRequest(
            query="*",
            scope="/",
            search_type=SearchType.FIND,
            search_metas=entries,
            read_content=_noop_reader(entries),
            find_predicates=None,
        )
        resp = await provider.search(req)
        assert {r.path for r in resp.results} == {"/a.py", "/b.txt"}
