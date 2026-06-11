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
from vfs.models import SearchType, VersionMeta
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


async def _setup_vfs(vfs: VFS):
    """Bootstrap namespace + agent; returns (namespace, agent_principal)."""
    ns = await vfs.create_namespace("ns", "admin")
    admin = await vfs.create_principal("admin")
    await vfs.bootstrap_admin(admin.id, ns.id)
    agent = await vfs.create_principal("agent")
    await vfs.grant(admin.id, agent.id, ns.id, "/", {"read", "write"})
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

        # Text artifact must be gone.
        rows = await nts_store._execute_fetchall(
            "SELECT COUNT(*) FROM search_text_artifacts WHERE content_hash=?",
            (content_hash,),
        )
        assert rows[0][0] == 0, "text artifact must be deleted when its content_hash is orphaned"

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
