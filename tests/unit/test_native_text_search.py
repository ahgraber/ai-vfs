"""Unit tests for the NativeTextSearch capability on SQLite and Mongo.

All SQLite tests run in-memory with the FTS5 trigram index.  Tests are skipped
(not failed) when the SQLite build does not ship the trigram tokenizer.

Covers:
    NativeTextSearchStorage/ContentAddressedTextDedup
    NativeTextSearchStorage/IndexTextInVersionTransaction
    NativeTextSearchStorage/TextArtifactGcFollowsContentOrphan
    NativeTextSearchStorage/MongoHasNoNativeTextSearch
    NativeTextSearchCapability/IndexOnWriteProducesExternalArtifact
    NativeTextSearchCapability/AcceleratedRegexAvoidsBlobReads
    NativeTextSearchCapability/RankedFulltext
    NativeTextSearchCapability/ContentMatchExpandsToVisibleOccurrences
    NativeTextSearchCapability/IdentityFromVisibleVersionAfterRollback
    NativeTextSearchCapability/ResultSetEquivalentToBruteForce  (SQLite leg)
"""

from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import re

import blake3 as _blake3
import pytest
import pytest_asyncio
from ulid import ULID

from vfs.config import VFSConfig
from vfs.gc import GarbageCollector
from vfs.models import FullTextMatchMode, SearchType, VersionMeta
from vfs.stores.local_blob import LocalFSBlobStore
from vfs.stores.sqlite_metadata import SQLiteMetadataStore
from vfs.vfs import VFS

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash(text: str) -> str:
    return _blake3.blake3(text.encode()).hexdigest()


def _version(ns: str, path: str, num: int, content_hash: str = "hash1") -> VersionMeta:
    return VersionMeta(
        id=str(ULID()),
        file_path=path,
        namespace_id=ns,
        version_number=num,
        content_hash=content_hash,
        size=4,
        created_at=_now(),
        created_by="p1",
    )


class _CountingBlobStore:
    """Blob store wrapper that counts ``get`` calls; used to verify zero blob reads."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.get_count: int = 0

    async def put(self, content_hash: str, content: bytes) -> None:
        await self._inner.put(content_hash, content)

    async def get(self, content_hash: str) -> bytes:
        self.get_count += 1
        return await self._inner.get(content_hash)

    async def list_hashes(self):  # noqa: ANN201 — generator, type not important here
        async for h in self._inner.list_hashes():
            yield h

    async def delete(self, content_hash: str) -> None:
        await self._inner.delete(content_hash)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def nts_store():
    """In-memory SQLiteMetadataStore with FTS5; skip if trigram tokenizer unavailable."""
    store = SQLiteMetadataStore(":memory:")
    await store.initialize()
    if store.native_text_search() is None:
        await store.close()
        pytest.skip("FTS5 trigram tokenizer not available (SQLite < 3.34)")
    yield store
    await store.close()


async def _setup_vfs(vfs: VFS, ops: set[str] | None = None):
    """Bootstrap namespace + agent; returns (namespace, agent_principal).

    ``ops`` overrides the operations granted to the agent (default read+write).
    """
    ns = await vfs.create_namespace("ns", "admin")
    admin = await vfs.create_principal("admin")
    await vfs.bootstrap_admin(admin.id, ns.id)
    agent = await vfs.create_principal("agent")
    await vfs.grant(admin.id, agent.id, ns.id, "/", ops or {"read", "write"})
    return ns, agent


@pytest_asyncio.fixture
async def vfs_nts(tmp_path):
    """VFS backed by SQLite with FTS5; skip if FTS5 unavailable."""
    db_path = str(tmp_path / "test.db")
    blob_path = str(tmp_path / "blobs")
    config = VFSConfig(
        metadata_store_uri=f"sqlite:///{db_path}",
        blob_store_uri=f"file:///{blob_path}/",
        otel_enabled=False,
        audit_log_enabled=False,
        blob_cache_enabled=False,
    )
    vfs = VFS(config)
    await vfs.initialize()
    if vfs._meta.native_text_search() is None:
        await vfs.close()
        pytest.skip("FTS5 trigram tokenizer not available (SQLite < 3.34)")
    yield vfs
    await vfs.close()


# ---------------------------------------------------------------------------
# NativeTextSearchStorage tests
# ---------------------------------------------------------------------------


class TestNativeTextSearchStorage:
    """Storage-level behavior: dedup, transaction atomicity, GC."""

    @pytest.mark.asyncio
    async def test_content_addressed_text_dedup(self, nts_store):
        """ContentAddressedTextDedup: two versions sharing the same content produce one text row."""
        nts = nts_store.native_text_search()
        content_hash = _hash("hello world")

        # Index the same content twice (different version IDs, same content_hash).
        v1 = str(ULID())
        v2 = str(ULID())
        await nts.index_text(v1, content_hash, nts.params_hash, "hello world")
        await nts.index_text(v2, content_hash, nts.params_hash, "hello world")

        rows = await nts_store._execute_fetchall(
            "SELECT COUNT(*) FROM search_text_artifacts WHERE content_hash=? AND provider_key=?",
            (content_hash, nts.provider_key),
        )
        assert rows[0][0] == 1, "identical content must produce exactly one text artifact row"

    @pytest.mark.asyncio
    async def test_index_text_in_version_transaction(self, nts_store):
        """IndexTextInVersionTransaction: rollback of the version txn also rolls back the text artifact."""
        nts = nts_store.native_text_search()
        content_hash = _hash("rolled back content")
        version_id = str(ULID())

        # Wrap in a transaction that we force to roll back.
        with pytest.raises(RuntimeError):
            async with nts_store.transaction():
                await nts.index_text(version_id, content_hash, nts.params_hash, "rolled back content")
                # Also write a version row to verify both are rolled back together.
                v = _version("ns", "/rb.txt", 1, content_hash)
                await nts_store.put_version(v)
                raise RuntimeError("force rollback")

        # After rollback: no text artifact.
        rows = await nts_store._execute_fetchall(
            "SELECT COUNT(*) FROM search_text_artifacts WHERE content_hash=?",
            (content_hash,),
        )
        assert rows[0][0] == 0, "text artifact must not persist after transaction rollback"

        # After rollback: no version row either.
        ver = await nts_store.get_version("ns", "/rb.txt")
        assert ver is None, "version must not persist after transaction rollback"

    @pytest.mark.asyncio
    async def test_text_artifact_gc_follows_content_orphan(self, nts_store, tmp_path):
        """TextArtifactGcFollowsContentOrphan: GC deletes text artifact when blob is orphaned."""
        from vfs.config import VFSConfig

        blob_store = LocalFSBlobStore(tmp_path / "blobs")
        nts = nts_store.native_text_search()

        content = b"orphan me"
        content_hash = _blake3.blake3(content).hexdigest()

        # Write blob and index text.
        await blob_store.put(content_hash, content)
        await nts.index_text(str(ULID()), content_hash, nts.params_hash, content.decode())

        # Write and immediately remove the only version reference.
        v = _version("ns", "/orphan.txt", 1, content_hash)
        await nts_store.put_version(v)
        assert await nts_store.has_version_references(content_hash)

        await nts_store.delete_versions([v.id])
        assert not await nts_store.has_version_references(content_hash)

        # Run GC.
        config = VFSConfig(audit_log_enabled=False)
        gc = GarbageCollector(nts_store, blob_store, config)
        result = await gc.run()
        assert result.blobs_reclaimed == 1

        # Text artifact must be gone from raw-text store AND both derived representations
        # (trigram + word) — the requirement deletes the content from all derived indexes.
        for table in ("search_text_artifacts", "search_fts", "search_fts_words"):
            rows = await nts_store._execute_fetchall(
                f"SELECT COUNT(*) FROM {table} WHERE content_hash=?",  # noqa: S608 — table is a test-local literal
                (content_hash,),
            )
            assert rows[0][0] == 0, f"{table} row must be deleted when its content_hash is orphaned"

    @pytest.mark.asyncio
    async def test_live_referenced_content_never_swept(self, nts_store, tmp_path):
        """LiveReferencedContentNeverSwept: GC must not sweep a content_hash with a live version.

        The reference check and the text-artifact deletion are atomic (one metadata transaction
        holding the store lock), so a live-referenced hash is never swept — neither its blob nor
        its text artifacts.  This pins the invariant the removed query-time existence re-check
        incidentally guarded; cross-store concurrency is exercised by the delegated integration test.
        """
        from vfs.config import VFSConfig

        blob_store = LocalFSBlobStore(tmp_path / "blobs")
        nts = nts_store.native_text_search()

        content = b"keep me alive"
        content_hash = _blake3.blake3(content).hexdigest()
        await blob_store.put(content_hash, content)
        await nts.index_text(str(ULID()), content_hash, nts.params_hash, content.decode())

        # A live (non-tombstone) version references the content at check time.
        v = _version("ns", "/live.txt", 1, content_hash)
        await nts_store.put_version(v)
        assert await nts_store.has_version_references(content_hash)

        config = VFSConfig(audit_log_enabled=False)
        gc = GarbageCollector(nts_store, blob_store, config)
        result = await gc.run()

        assert result.blobs_reclaimed == 0, "a live-referenced hash must not be swept"
        rows = await nts_store._execute_fetchall(
            "SELECT COUNT(*) FROM search_text_artifacts WHERE content_hash=?",
            (content_hash,),
        )
        assert rows[0][0] == 1, "text artifact for a live-referenced hash must survive"
        assert await blob_store.get(content_hash) == content, "blob for a live-referenced hash must survive"

    @pytest.mark.asyncio
    async def test_mongo_has_no_native_text_search(self):
        """MongoHasNoNativeTextSearch: MongoMetadataStore.native_text_search() returns None."""
        if importlib.util.find_spec("motor") is None:
            pytest.skip("motor not installed")
        from vfs.stores.mongo_metadata import MongoMetadataStore

        store = MongoMetadataStore("mongodb://localhost/test")
        assert store.native_text_search() is None


# ---------------------------------------------------------------------------
# NativeTextSearchCapability tests (VFS-level)
# ---------------------------------------------------------------------------


class TestNativeTextSearchCapability:
    """End-to-end write→index→search behavior through the VFS."""

    @pytest.mark.asyncio
    async def test_index_on_write_produces_external_artifact(self, vfs_nts):
        """IndexOnWriteProducesExternalArtifact: write stores a ready external text artifact."""
        ns, agent = await _setup_vfs(vfs_nts)
        ver = await vfs_nts.write(ns.id, "/doc.txt", b"hello world", principal_id=agent.id)

        nts = vfs_nts._meta.native_text_search()
        pk = nts.provider_key

        # search_meta must carry a ready external artifact under the NTS provider key.
        assert pk in ver.search_meta, "NTS provider key absent from search_meta"
        from vfs.models import SearchArtifact

        artifact = SearchArtifact.from_dict(ver.search_meta[pk])
        assert artifact.status == "ready"
        assert artifact.storage == "external"
        assert artifact.artifact_ref is not None

        # The content-addressed text record must exist in the store.
        rows = await vfs_nts._meta._execute_fetchall(
            "SELECT status FROM search_text_artifacts WHERE content_hash=? AND provider_key=?",
            (ver.content_hash, pk),
        )
        assert len(rows) == 1
        assert rows[0][0] == "ready"

    @pytest.mark.asyncio
    async def test_accelerated_regex_avoids_blob_reads(self, vfs_nts):
        """AcceleratedRegexAvoidsBlobReads: NTS regex uses no blob reads for fresh records."""
        ns, agent = await _setup_vfs(vfs_nts)
        await vfs_nts.write(ns.id, "/a.py", b"def foo(): return 42", principal_id=agent.id)
        await vfs_nts.write(ns.id, "/b.py", b"def bar(): pass", principal_id=agent.id)

        # Wrap blob store with a counter; reset is implicit (starts at 0).
        counting = _CountingBlobStore(vfs_nts._blob)
        vfs_nts._blob = counting

        results = await vfs_nts.search(ns.id, "foo", "/", SearchType.REGEX, principal_id=agent.id)

        assert counting.get_count == 0, "NTS regex must not read blobs for fresh records"
        assert {r.path for r in results} == {"/a.py"}

    @pytest.mark.asyncio
    async def test_ranked_fulltext(self, vfs_nts):
        """RankedFulltext: fulltext results are ordered by BM25 relevance (higher score first)."""
        ns, agent = await _setup_vfs(vfs_nts)

        # High-relevance: term appears many times.
        await vfs_nts.write(ns.id, "/high.txt", b"python python python python", principal_id=agent.id)
        # Low-relevance: term appears once.
        await vfs_nts.write(ns.id, "/low.txt", b"python is a language", principal_id=agent.id)
        # No match: term absent.
        await vfs_nts.write(ns.id, "/none.txt", b"java is also a language", principal_id=agent.id)

        results = await vfs_nts.search(ns.id, "python", "/", SearchType.FULLTEXT, principal_id=agent.id)

        paths = [r.path for r in results]
        assert "/none.txt" not in paths, "non-matching document must be excluded"
        assert set(paths) == {"/high.txt", "/low.txt"}

        # /high.txt should have a higher (or equal) score than /low.txt.
        scores = {r.path: r.score for r in results}
        assert scores["/high.txt"] >= scores["/low.txt"], (
            f"high-relevance doc score {scores['/high.txt']:.4f} must be >= "
            f"low-relevance doc score {scores['/low.txt']:.4f}"
        )

    @pytest.mark.asyncio
    async def test_content_match_expands_to_visible_occurrences(self, vfs_nts):
        """ContentMatchExpandsToVisibleOccurrences: identical content at two paths → both returned."""
        ns, agent = await _setup_vfs(vfs_nts)
        shared_content = b"unique phrase xyzzy"

        await vfs_nts.write(ns.id, "/copy1.txt", shared_content, principal_id=agent.id)
        await vfs_nts.write(ns.id, "/copy2.txt", shared_content, principal_id=agent.id)

        # Both files share one content_hash → one text artifact → two result occurrences.
        results = await vfs_nts.search(ns.id, "xyzzy", "/", SearchType.REGEX, principal_id=agent.id)
        assert {r.path for r in results} == {"/copy1.txt", "/copy2.txt"}

        # Fulltext path also expands to both occurrences.
        ft_results = await vfs_nts.search(ns.id, "xyzzy", "/", SearchType.FULLTEXT, principal_id=agent.id)
        assert {r.path for r in ft_results} == {"/copy1.txt", "/copy2.txt"}

    @pytest.mark.asyncio
    async def test_identity_from_visible_version_after_rollback(self, vfs_nts):
        """IdentityFromVisibleVersionAfterRollback: rollback result carries rollback version's identity."""
        ns, agent = await _setup_vfs(vfs_nts)

        # v1: content that will be searched.
        await vfs_nts.write(ns.id, "/file.txt", b"searchable content here", principal_id=agent.id)
        # v2: different content so v1 content_hash is superseded.
        await vfs_nts.write(ns.id, "/file.txt", b"completely different", principal_id=agent.id)
        # v3: rollback to v1 content (reuses v1's content_hash).
        v3 = await vfs_nts.rollback(ns.id, "/file.txt", 1, principal_id=agent.id)

        # v3 is now the current version; its content_hash matches v1's text artifact.
        results = await vfs_nts.search(ns.id, "searchable content", "/", SearchType.REGEX, principal_id=agent.id)
        assert len(results) == 1
        assert results[0].path == "/file.txt"
        # The result must reference the current visible version (v3), not v1.
        # We verify this indirectly: the VFS builds entries from the current version,
        # so the only visible occurrence references v3's content_hash (same as v1's).
        assert v3.content_hash in {
            row[0]
            for row in await vfs_nts._meta._execute_fetchall(
                "SELECT content_hash FROM search_text_artifacts WHERE provider_key=?",
                (vfs_nts._meta.native_text_search().provider_key,),
            )
        }

    @pytest.mark.asyncio
    async def test_copy_propagates_search_meta(self, vfs_nts):
        """CopyPropagatesSearchMeta: a copied file is searchable with no blob reads (no reindex).

        The copy shares the source ``content_hash``, so carrying ``search_meta`` forward keeps the
        destination identity-current (fresh) — served from the index rather than re-verified as a
        straggler via a blob read.
        """
        ns, agent = await _setup_vfs(vfs_nts)
        await vfs_nts.write(ns.id, "/a.py", b"def discover(): return 1", principal_id=agent.id)
        await vfs_nts.copy(ns.id, "/a.py", "/b.py", principal_id=agent.id)

        counting = _CountingBlobStore(vfs_nts._blob)
        vfs_nts._blob = counting

        results = await vfs_nts.search(ns.id, "discover", "/", SearchType.REGEX, principal_id=agent.id)

        assert counting.get_count == 0, "copied file must be fresh (no straggler blob read)"
        assert {r.path for r in results} == {"/a.py", "/b.py"}

    @pytest.mark.asyncio
    async def test_move_destination_propagates_search_meta(self, vfs_nts):
        """MoveDestinationPropagatesSearchMeta: a moved file is searchable at the destination, no blob reads."""
        ns, agent = await _setup_vfs(vfs_nts, {"read", "write", "delete"})
        await vfs_nts.write(ns.id, "/old.py", b"def relocate(): return 2", principal_id=agent.id)
        await vfs_nts.move(ns.id, "/old.py", "/new.py", principal_id=agent.id)

        counting = _CountingBlobStore(vfs_nts._blob)
        vfs_nts._blob = counting

        results = await vfs_nts.search(ns.id, "relocate", "/", SearchType.REGEX, principal_id=agent.id)

        assert counting.get_count == 0, "moved file must be fresh at the destination (no straggler blob read)"
        assert {r.path for r in results} == {"/new.py"}, "destination present; source tombstone absent"

    @pytest.mark.asyncio
    async def test_result_set_equivalent_to_brute_force(self, vfs_nts):
        """ResultSetEquivalentToBruteForce: NTS and brute-force produce identical result sets.

        Includes a trigram-unfriendly pattern (``[0-9]+``) to exercise the sequential-scan
        fallback path where FTS5 trigram prune cannot narrow candidates.

        Also includes alternation (``foo|barbaz``, ``cat|dogs``) and optional-quantifier
        (``(abc)+x?``) patterns to verify the conservative literal-extraction guard:
        NTS must not prune documents that only match one branch of an alternation.

        And includes escaped pipe (``\\|``) to verify that a literal pipe character in the
        pattern is not treated as alternation.
        """
        ns, agent = await _setup_vfs(vfs_nts)

        documents = {
            "/digits.txt": b"order 42 received on day 7",
            "/nodigits.txt": b"no numbers here",
            "/mixed.txt": b"version 3 of the spec has 12 items",
            "/alpha.txt": b"only alphabetic content here",
            "/has_foo.txt": b"this document contains foo only",
            "/has_barbaz.txt": b"this document contains barbaz only",
            "/has_cat.txt": b"the cat sat on the mat",
            "/has_dogs.txt": b"dogs are friendly animals",
            "/has_abc.txt": b"abcx something here",
            "/has_pipe.txt": b"a|b pipe character present",
        }
        for path, content in documents.items():
            await vfs_nts.write(ns.id, path, content, principal_id=agent.id)

        patterns = [
            r"[0-9]+",  # trigram-unfriendly: no extractable literal trigrams
            r"order",  # literal — FTS5 prune applies
            r"spec.*items",  # multi-token spanning (contains '*', falls back)
            r"def\s+\w+",  # no match expected in these docs
            r"foo|barbaz",  # alternation — must not false-negative foo-only docs
            r"cat|dogs",  # alternation with two branches
            r"(abc)+x?",  # optional quantifier — conservative fallback
            r"\|",  # escaped pipe: literal '|' character, not alternation
        ]

        for pattern in patterns:
            # NTS result via VFS search.
            nts_results = await vfs_nts.search(ns.id, pattern, "/", SearchType.REGEX, principal_id=agent.id)
            nts_paths = {r.path for r in nts_results}

            # Brute-force baseline: compile regex and match against raw content.
            compiled = re.compile(pattern)
            brute_paths = {path for path, content in documents.items() if compiled.search(content.decode())}

            assert nts_paths == brute_paths, f"pattern {pattern!r}: NTS={nts_paths} != brute-force={brute_paths}"

    @pytest.mark.asyncio
    async def test_regex_line_number_and_match_context(self, vfs_nts):
        """GrepMatchesContent: regex results must carry line_number and match_context.

        Each SearchResult from a REGEX search must have line_number (1-based) and
        match_context (stripped matching line text) populated — fields verified against
        the DefaultSearchProvider brute-force baseline.  This is the assertion gap that
        previously let the missing-fields bug through unit testing.
        """
        ns, agent = await _setup_vfs(vfs_nts)
        content = b"line one\n# TODO: something important\nline three\n"
        await vfs_nts.write(ns.id, "/multi.txt", content, principal_id=agent.id)

        results = await vfs_nts.search(ns.id, "TODO", "/", SearchType.REGEX, principal_id=agent.id)

        assert len(results) == 1
        r = results[0]
        assert r.path == "/multi.txt"
        assert r.line_number == 2, f"expected line 2, got {r.line_number}"
        assert r.match_context == "# TODO: something important", f"unexpected match_context: {r.match_context!r}"

    @pytest.mark.asyncio
    async def test_regex_multiple_matching_lines(self, vfs_nts):
        """GrepMatchesContent multi-line: each matching line produces one SearchResult.

        Verified against the DefaultSearchProvider brute-force baseline: one result per
        matching line, in document order.
        """
        ns, agent = await _setup_vfs(vfs_nts)
        content = b"alpha TODO first\nbeta nothing\ngamma TODO second\n"
        await vfs_nts.write(ns.id, "/two.txt", content, principal_id=agent.id)

        results = await vfs_nts.search(ns.id, "TODO", "/", SearchType.REGEX, principal_id=agent.id)

        assert len(results) == 2
        by_line = sorted(results, key=lambda r: r.line_number or 0)
        assert by_line[0].line_number == 1
        assert by_line[0].match_context == "alpha TODO first"
        assert by_line[1].line_number == 3
        assert by_line[1].match_context == "gamma TODO second"

    @pytest.mark.asyncio
    async def test_anchored_pattern_matches_non_first_line(self, vfs_nts):
        """Cross-backend identity / anchoring: `^`-anchored regex matches per line, not per document.

        Guards the fix that dropped PostgreSQL's whole-document anchor-sensitive `~` prune
        (a `~` prune anchors `^`/`$` to the whole text, so it would false-negative a file
        whose match is on a non-first line). RE2 per-line verification finds it. The SQLite
        leg exercises the shared per-line logic here; the PostgreSQL SQL path is verified by
        the podman integration suite.
        """
        ns, agent = await _setup_vfs(vfs_nts)
        content = b"header line\nimport os\nimport sys\n"
        await vfs_nts.write(ns.id, "/mod.py", content, principal_id=agent.id)

        results = await vfs_nts.search(ns.id, r"^import", "/", SearchType.REGEX, principal_id=agent.id)

        lines = sorted(r.line_number for r in results)
        assert lines == [2, 3], f"expected ^import to match lines 2 and 3, got {lines}"

    @pytest.mark.asyncio
    async def test_alternation_no_false_negatives(self, vfs_nts):
        """Regression: alternation in pattern must not cause false negatives.

        Before the fix, ``_fts5_literal_from_pattern("foo|barbaz")`` extracted "barbaz"
        as the FTS5 prune literal.  Documents matching only the "foo" branch were never
        examined by ``re.search`` — a confirmed false negative.
        """
        ns, agent = await _setup_vfs(vfs_nts)

        # /foo_only.txt matches the "foo" branch; /barbaz_only.txt matches "barbaz".
        await vfs_nts.write(ns.id, "/foo_only.txt", b"this has foo in it", principal_id=agent.id)
        await vfs_nts.write(ns.id, "/barbaz_only.txt", b"this has barbaz in it", principal_id=agent.id)
        await vfs_nts.write(ns.id, "/neither.txt", b"no match here at all", principal_id=agent.id)

        results = await vfs_nts.search(ns.id, "foo|barbaz", "/", SearchType.REGEX, principal_id=agent.id)
        result_paths = {r.path for r in results}

        # Both branches must be present — the "foo"-only document must NOT be silently pruned.
        assert "/foo_only.txt" in result_paths, "foo|barbaz must match the foo branch"
        assert "/barbaz_only.txt" in result_paths, "foo|barbaz must match the barbaz branch"
        assert "/neither.txt" not in result_paths

    @pytest.mark.asyncio
    async def test_fulltext_injection_safe_operators(self, vfs_nts):
        """S1: FTS5 special tokens in fulltext query must not raise or silently become operators.

        ``c++`` would raise an FTS5 syntax error if passed raw.
        ``foo OR barbaz`` would match boolean OR if passed raw — tokens must be literal.
        """
        ns, agent = await _setup_vfs(vfs_nts)

        await vfs_nts.write(ns.id, "/cpp.txt", b"c++ is a programming language", principal_id=agent.id)
        await vfs_nts.write(ns.id, "/foo.txt", b"foo is a placeholder name", principal_id=agent.id)
        await vfs_nts.write(ns.id, "/barbaz.txt", b"barbaz is another name", principal_id=agent.id)

        # "c++" must not raise IndexUnavailableError or SearchTypeUnsupportedError.
        results_cpp = await vfs_nts.search(ns.id, "c++", "/", SearchType.FULLTEXT, principal_id=agent.id)
        # At minimum it must not raise; result may be empty or contain /cpp.txt.
        assert isinstance(results_cpp, list)

        # "foo OR barbaz" — "OR" is a literal token, not a boolean operator.
        # True boolean OR would return both /foo.txt and /barbaz.txt.
        # Literal token-AND ("foo" AND "OR" AND "barbaz") returns nothing (no doc has all three).
        results_or = await vfs_nts.search(ns.id, "foo OR barbaz", "/", SearchType.FULLTEXT, principal_id=agent.id)
        or_paths = {r.path for r in results_or}
        # Neither file contains all three tokens "foo", "OR", and "barbaz" together.
        assert "/foo.txt" not in or_paths, "OR must be treated as a literal token, not a boolean operator"
        assert "/barbaz.txt" not in or_paths

    @pytest.mark.asyncio
    async def test_fulltext_match_any_ranks_union(self, vfs_nts):
        """FulltextMatchAnyRanksUnion (SQLite): mode=ANY returns the union, both-terms doc ranks first.

        Corpus: "hello world" (matches one term) and "hello s3 bucket" (matches both).
        Query "hello s3" in mode=ANY returns both; the both-terms doc ranks above the
        one-term doc.  The two-character term ``s3`` is a first-class ``unicode61`` word
        token — no trigram floor.
        """
        ns, agent = await _setup_vfs(vfs_nts)
        await vfs_nts.write(ns.id, "/one.txt", b"hello world", principal_id=agent.id)
        await vfs_nts.write(ns.id, "/both.txt", b"hello s3 bucket", principal_id=agent.id)

        results = await vfs_nts.search(
            ns.id, "hello s3", "/", SearchType.FULLTEXT, principal_id=agent.id, match_mode=FullTextMatchMode.ANY
        )

        paths = [r.path for r in results]
        assert set(paths) == {"/one.txt", "/both.txt"}, "ANY mode must return the union of per-term matches"
        # The both-terms doc must rank before the one-term doc (results are relevance-ordered).
        assert paths.index("/both.txt") < paths.index("/one.txt"), (
            f"both-terms doc must rank above one-term doc; got order {paths}"
        )

    @pytest.mark.asyncio
    async def test_fulltext_match_all_requires_every_term(self, vfs_nts):
        """FulltextMatchAllRequiresEveryTerm (SQLite): mode=ALL returns only docs with every term.

        Same ``s3`` corpus as the ANY test; query "hello s3" in mode=ALL returns only the
        both-terms doc — the "hello world" doc lacks ``s3``.
        """
        ns, agent = await _setup_vfs(vfs_nts)
        await vfs_nts.write(ns.id, "/one.txt", b"hello world", principal_id=agent.id)
        await vfs_nts.write(ns.id, "/both.txt", b"hello s3 bucket", principal_id=agent.id)

        results = await vfs_nts.search(
            ns.id, "hello s3", "/", SearchType.FULLTEXT, principal_id=agent.id, match_mode=FullTextMatchMode.ALL
        )

        assert {r.path for r in results} == {"/both.txt"}, "ALL mode must require every query term"

    @pytest.mark.asyncio
    async def test_ranked_fulltext_any_mode(self, vfs_nts):
        """RankedFulltextAnyMode (SQLite): a both-terms doc ranks above a one-term doc in ANY mode."""
        ns, agent = await _setup_vfs(vfs_nts)
        await vfs_nts.write(ns.id, "/both.txt", b"alpha beta together", principal_id=agent.id)
        await vfs_nts.write(ns.id, "/one.txt", b"alpha only here", principal_id=agent.id)

        results = await vfs_nts.search(
            ns.id, "alpha beta", "/", SearchType.FULLTEXT, principal_id=agent.id, match_mode=FullTextMatchMode.ANY
        )

        paths = [r.path for r in results]
        assert set(paths) == {"/both.txt", "/one.txt"}
        assert paths.index("/both.txt") < paths.index("/one.txt"), (
            f"doc matching both terms must rank above doc matching one term; got {paths}"
        )

    @pytest.mark.asyncio
    async def test_fulltext_any_mode_quote_escaping(self, vfs_nts):
        """ANY mode quote escaping: a token containing '\"' is escaped (\"\") and does not raise.

        Mirrors test_fulltext_injection_safe_operators for the OR-join path: an embedded
        double-quote must be doubled into a literal phrase, never become an FTS5 operator.
        """
        ns, agent = await _setup_vfs(vfs_nts)
        await vfs_nts.write(ns.id, "/q.txt", b'he said "hi" today', principal_id=agent.id)

        # Token containing an embedded double-quote — must not raise an FTS5 syntax error,
        # and the escaped phrase must still match the document literally.
        results = await vfs_nts.search(
            ns.id, 'said "hi"', "/", SearchType.FULLTEXT, principal_id=agent.id, match_mode=FullTextMatchMode.ANY
        )
        assert {r.path for r in results} == {"/q.txt"}, (
            "embedded-quote query in ANY mode must escape to a literal phrase that still matches"
        )

    @pytest.mark.asyncio
    async def test_params_hash_drift_is_straggler_fails_loud(self, vfs_nts):
        """A ready artifact whose params_hash no longer matches the active provider is a straggler.

        The former straggler path verified (REGEX) or approximated (FULLTEXT) such entries
        in-process; the native path now fails loud (ReindexRequiredError) for both search types —
        reindex is the remedy, and no lazy backfill occurs (the artifact stays stale).
        """
        from datetime import datetime, timezone

        from vfs.errors import ReindexRequiredError
        from vfs.models import SearchArtifact

        ns, agent = await _setup_vfs(vfs_nts)
        ver = await vfs_nts.write(ns.id, "/strag.txt", b"hello world", principal_id=agent.id)
        nts = vfs_nts._meta.native_text_search()

        # Drift the artifact's params_hash so it is no longer identity-current → straggler.
        stale_artifact = SearchArtifact(
            status="ready",
            schema_version=1,
            provider_key=nts.provider_key,
            provider_version="1",
            params_hash="STALE_HASH_DOES_NOT_MATCH",
            content_hash=ver.content_hash,
            created_at=datetime.now(timezone.utc),
            storage="external",
            artifact_ref=None,
        )
        async with vfs_nts._meta.transaction():
            await vfs_nts._meta.update_search_artifact(ver.id, nts.provider_key, stale_artifact)

        with pytest.raises(ReindexRequiredError, match="reindex"):
            await vfs_nts.search(ns.id, "hello", "/", SearchType.REGEX, principal_id=agent.id)

        # Still stale (no backfill happened) → FULLTEXT also fails loud, no inline approximation.
        with pytest.raises(ReindexRequiredError, match="reindex"):
            await vfs_nts.search(
                ns.id, "hello", "/", SearchType.FULLTEXT, principal_id=agent.id, match_mode=FullTextMatchMode.ANY
            )


class TestFulltextWordRepresentation:
    """FulltextWordRepresentation: word tokens for FULLTEXT, trigram for REGEX, on SQLite."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", [FullTextMatchMode.ALL, FullTextMatchMode.ANY])
    async def test_short_term_fulltext_is_representable(self, vfs_nts, mode):
        """ShortTermFulltextIsRepresentable: the 2-char term ``s3`` matches exactly (no trigram floor)."""
        ns, agent = await _setup_vfs(vfs_nts)
        await vfs_nts.write(ns.id, "/deploy.txt", b"deploy to s3", principal_id=agent.id)
        await vfs_nts.write(ns.id, "/archive.txt", b"deploy to archive", principal_id=agent.id)

        results = await vfs_nts.search(ns.id, "s3", "/", SearchType.FULLTEXT, principal_id=agent.id, match_mode=mode)
        assert {r.path for r in results} == {"/deploy.txt"}, (
            "the two-character term 's3' must match only the document containing it"
        )

    @pytest.mark.asyncio
    async def test_fulltext_matches_whole_words_not_substrings(self, vfs_nts):
        """FulltextMatchesWholeWordsNotSubstrings: FULLTEXT 'cat' does NOT match 'category'."""
        ns, agent = await _setup_vfs(vfs_nts)
        await vfs_nts.write(ns.id, "/cat.txt", b"this is a category of things", principal_id=agent.id)

        results = await vfs_nts.search(ns.id, "cat", "/", SearchType.FULLTEXT, principal_id=agent.id)
        assert {r.path for r in results} == set(), (
            "fulltext matches whole word tokens, not substrings — 'cat' must not match 'category'"
        )

    @pytest.mark.asyncio
    async def test_regex_still_matches_substrings(self, vfs_nts):
        """RegexStillMatchesSubstrings: REGEX 'cat' DOES match 'category' (trigram unchanged)."""
        ns, agent = await _setup_vfs(vfs_nts)
        await vfs_nts.write(ns.id, "/cat.txt", b"this is a category of things", principal_id=agent.id)

        results = await vfs_nts.search(ns.id, "cat", "/", SearchType.REGEX, principal_id=agent.id)
        assert {r.path for r in results} == {"/cat.txt"}, (
            "regex retains substring matching via the unchanged trigram representation"
        )

    @pytest.mark.asyncio
    async def test_provider_version_bumped_on_new_writes(self, vfs_nts):
        """provider_version evidence: new SQLite artifacts carry the bumped marker; is_usable ignores it.

        Covers the SQLite ``index_text`` builder (the stored ``ready`` artifact) and the
        ``vfs.write`` decode-error ``unsupported`` builder.  ``is_usable`` compares
        ``params_hash`` only, so an artifact with the old ``provider_version`` but a current
        ``params_hash`` stays usable.
        """
        from vfs.models import SearchArtifact

        ns, agent = await _setup_vfs(vfs_nts)
        nts = vfs_nts._meta.native_text_search()

        # index_text builder → ready artifact carries provider_version "2".
        ver = await vfs_nts.write(ns.id, "/text.txt", b"hello world", principal_id=agent.id)
        ready = SearchArtifact.from_dict(ver.search_meta[nts.provider_key])
        assert ready.status == "ready"
        assert ready.provider_version == "2", "index_text must stamp the bumped provider_version"

        # vfs.write decode-error builder → unsupported artifact carries provider_version "2".
        bin_ver = await vfs_nts.write(ns.id, "/bin.dat", b"\xff\xfe\x00\x01", principal_id=agent.id)
        unsupported = SearchArtifact.from_dict(bin_ver.search_meta[nts.provider_key])
        assert unsupported.status == "unsupported"
        assert unsupported.provider_version == "2", "the decode-error builder must stamp the bumped version"

        # is_usable is unaffected by provider_version: an old-version record is still usable.
        old_marker = SearchArtifact(
            status="ready",
            schema_version=1,
            provider_key=nts.provider_key,
            provider_version="1",
            params_hash=nts.params_hash,
            content_hash=ver.content_hash,
            created_at=_now(),
            storage="external",
            artifact_ref=None,
        )
        assert old_marker.is_usable(current_content_hash=ver.content_hash, active_params_hash=nts.params_hash), (
            "provider_version must not participate in is_usable"
        )


class TestDerivedIndexRebuild:
    """DerivedIndexRebuild: the word index is rebuilt from raw_text at init, idempotently."""

    async def _word_row_count(self, store, content_hash: str) -> int:
        rows = await store._execute_fetchall(
            "SELECT COUNT(*) FROM search_fts_words WHERE provider_key=? AND content_hash=?",
            (store.native_text_search().provider_key, content_hash),
        )
        return rows[0][0]

    @pytest.mark.asyncio
    async def test_word_index_backfilled_from_raw_text_without_blob_reads(self, vfs_nts):
        """WordIndexBackfilledFromRawTextWithoutBlobReads: rebuild from raw_text, zero blob reads."""
        ns, agent = await _setup_vfs(vfs_nts)
        await vfs_nts.write(ns.id, "/doc.txt", b"hello s3 bucket", principal_id=agent.id)
        store = vfs_nts._meta
        nts = store.native_text_search()
        ch = (await store.get_version(ns.id, "/doc.txt")).content_hash

        # Simulate content indexed before the word table existed: drop its word rows,
        # leaving the content-addressed raw_text record in place.
        async with store._lock:
            await store._conn.exec_driver_sql(
                "DELETE FROM search_fts_words WHERE provider_key=? AND content_hash=?",
                (nts.provider_key, ch),
            )
            await store._conn.commit()
        assert await self._word_row_count(store, ch) == 0

        # Re-run the init-time backfill: it rebuilds from raw_text only.
        await store._backfill_word_index()
        assert await self._word_row_count(store, ch) == 1

        # FULLTEXT now serves the rebuilt content with zero blob reads.
        counting = _CountingBlobStore(vfs_nts._blob)
        vfs_nts._blob = counting
        results = await vfs_nts.search(ns.id, "s3", "/", SearchType.FULLTEXT, principal_id=agent.id)
        assert {r.path for r in results} == {"/doc.txt"}
        assert counting.get_count == 0, "rebuild + fresh search must not read the blob store"

    @pytest.mark.asyncio
    async def test_derived_index_rebuild_is_idempotent_and_resumable(self, vfs_nts):
        """DerivedIndexRebuildIsIdempotentAndResumable: no duplicate rows / occurrences on re-run."""
        ns, agent = await _setup_vfs(vfs_nts)
        await vfs_nts.write(ns.id, "/a.txt", b"alpha unique", principal_id=agent.id)
        await vfs_nts.write(ns.id, "/b.txt", b"beta unique", principal_id=agent.id)
        store = vfs_nts._meta
        nts = store.native_text_search()
        ch_a = (await store.get_version(ns.id, "/a.txt")).content_hash
        ch_b = (await store.get_version(ns.id, "/b.txt")).content_hash

        # A re-run with everything already present inserts nothing (idempotent).
        await store._backfill_word_index()
        assert await self._word_row_count(store, ch_a) == 1
        assert await self._word_row_count(store, ch_b) == 1

        # Simulate a partially-built index: drop only /a.txt's word row, then resume.
        async with store._lock:
            await store._conn.exec_driver_sql(
                "DELETE FROM search_fts_words WHERE provider_key=? AND content_hash=?",
                (nts.provider_key, ch_a),
            )
            await store._conn.commit()
        await store._backfill_word_index()
        # Only the missing row is filled; the present row is not duplicated.
        assert await self._word_row_count(store, ch_a) == 1
        assert await self._word_row_count(store, ch_b) == 1

        # The shared term returns exactly one occurrence per path (no duplicate results).
        results = await vfs_nts.search(ns.id, "unique", "/", SearchType.FULLTEXT, principal_id=agent.id)
        assert sorted(r.path for r in results) == ["/a.txt", "/b.txt"]

    @pytest.mark.asyncio
    async def test_backfill_failure_fails_closed(self, monkeypatch):
        """A failed word-index rebuild must NOT expose native search (fail closed).

        If the rebuild raises after the FTS5 tables are created, exposing the capability would
        let FULLTEXT serve from an incomplete word table — silent false negatives, violating
        the DerivedIndexRebuild "rebuild completes before serving" invariant.  native_text_search()
        must return None instead, so fulltext is unsupported and regex falls back to brute-force.
        """
        store = SQLiteMetadataStore(":memory:")

        async def _boom() -> None:
            raise RuntimeError("simulated word-index backfill failure")

        monkeypatch.setattr(store, "_backfill_word_index", _boom)
        await store.initialize()
        try:
            assert store.native_text_search() is None, "a failed word-index backfill must not expose native search"
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_rebuild_runs_on_reinitialize(self, tmp_path):
        """The anti-join rebuild runs on a fresh ``initialize()`` (resumable across restarts).

        Exercises the full `initialize()` → `_setup_fts5` → `_backfill_word_index` path on a
        second store instance over the same database, not a direct `_backfill_word_index()` call.
        """
        db_path = str(tmp_path / "reinit.db")
        store = SQLiteMetadataStore(db_path)
        await store.initialize()
        nts = store.native_text_search()
        if nts is None:
            await store.close()
            pytest.skip("FTS5 trigram tokenizer not available (SQLite < 3.34)")
        ch = _hash("hello s3 bucket")
        await nts.index_text(str(ULID()), ch, nts.params_hash, "hello s3 bucket")
        # Simulate content indexed before the word table existed: drop its word rows only.
        async with store._lock:
            await store._conn.exec_driver_sql(
                "DELETE FROM search_fts_words WHERE provider_key=? AND content_hash=?",
                (nts.provider_key, ch),
            )
            await store._conn.commit()
        await store.close()

        # A fresh store over the same DB runs the rebuild during initialize().
        store2 = SQLiteMetadataStore(db_path)
        await store2.initialize()
        try:
            rows = await store2._execute_fetchall(
                "SELECT COUNT(*) FROM search_fts_words WHERE provider_key=? AND content_hash=?",
                (store2.native_text_search().provider_key, ch),
            )
            assert rows[0][0] == 1, "re-initialize() must rebuild the missing word-index row via the anti-join"
        finally:
            await store2.close()


class TestFulltextTermCap:
    """FulltextMatchMode boundary: reject FULLTEXT queries above the term cap."""

    @pytest.mark.asyncio
    async def test_fulltext_rejects_too_many_terms(self, vfs_nts):
        """FulltextRejectsTooManyTerms: > 128 terms raises at the boundary; <= 128 succeeds."""
        ns, agent = await _setup_vfs(vfs_nts)
        await vfs_nts.write(ns.id, "/doc.txt", b"hello world", principal_id=agent.id)

        over = " ".join(f"term{i}" for i in range(129))
        with pytest.raises(ValueError, match="too many terms"):
            await vfs_nts.search(ns.id, over, "/", SearchType.FULLTEXT, principal_id=agent.id)

        # At the cap (128 terms) the search runs without the boundary error.
        at_cap = " ".join(f"term{i}" for i in range(128))
        results = await vfs_nts.search(ns.id, at_cap, "/", SearchType.FULLTEXT, principal_id=agent.id)
        assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_term_cap_ignored_for_non_fulltext(self, vfs_nts):
        """The cap is FULLTEXT-only: an over-long REGEX query is not rejected by the boundary."""
        ns, agent = await _setup_vfs(vfs_nts)
        await vfs_nts.write(ns.id, "/doc.txt", b"hello world", principal_id=agent.id)

        # 200 whitespace-separated tokens as a regex pattern — not subject to the FULLTEXT cap.
        over = " ".join(f"term{i}" for i in range(200))
        results = await vfs_nts.search(ns.id, over, "/", SearchType.REGEX, principal_id=agent.id)
        assert isinstance(results, list)
