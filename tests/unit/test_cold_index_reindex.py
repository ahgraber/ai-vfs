"""Tests for cold-index failure/straggler handling and the reindex lifecycle.

All tests require SQLite with the FTS5 trigram tokenizer; they are skipped (not
failed) when that tokenizer is unavailable.

Covers:
    ColdIndexFailsLoud/FreshIndexCompleteNoBlobReads
    ColdIndexFailsLoud/AnyStragglerFailsLoud
    ColdIndexFailsLoud/DecidedNonMatchExcluded
    ColdIndexFailsLoud/IndexUnavailableFailsLoud
    ColdIndexFailsLoud/UndecodableContentIsUnsupported
    SearchMetaReindex/BatchReindex
    SearchMetaReindex/SearchPerformsNoLazyBackfill
    SearchMetaReindex/RollbackCopiesSearchMeta
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from vfs.config import VFSConfig
from vfs.errors import IndexUnavailableError, ReindexRequiredError
from vfs.models import SearchArtifact, SearchType
from vfs.protocols.search import SearchLimits
from vfs.vfs import VFS

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _CountingBlobStore:
    """Blob-store wrapper that counts ``get`` calls."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.get_count: int = 0

    async def put(self, content_hash: str, content: bytes) -> None:
        await self._inner.put(content_hash, content)

    async def get(self, content_hash: str) -> bytes:
        self.get_count += 1
        return await self._inner.get(content_hash)

    async def list_hashes(self):  # noqa: ANN201
        async for h in self._inner.list_hashes():
            yield h

    async def delete(self, content_hash: str) -> None:
        await self._inner.delete(content_hash)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def vfs_nts(tmp_path):
    """VFS backed by SQLite with FTS5; skip when FTS5 trigram tokenizer is unavailable."""
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


async def _setup_vfs(vfs: VFS):
    """Bootstrap namespace + agent; returns (namespace, agent_principal)."""
    ns = await vfs.create_namespace("ns", "admin")
    admin = await vfs.create_principal("admin")
    await vfs.bootstrap_admin(admin.id, ns.id)
    agent = await vfs.create_principal("agent")
    await vfs.grant(admin.id, agent.id, ns.id, "/", {"read", "write"})
    return ns, agent


async def _clear_search_meta(vfs: VFS, ns_id: str, path: str) -> None:
    """Remove all search artifacts from a version's search_meta (simulates un-indexed state)."""
    ver = await vfs._meta.get_version(ns_id, path)
    if ver:
        await vfs._meta.update_search_meta(ver.id, {})


# ---------------------------------------------------------------------------
# ColdIndexFailsLoud/FreshIndexCompleteNoBlobReads
# ---------------------------------------------------------------------------


class TestFreshIndexCompleteNoBlobReads:
    @pytest.mark.asyncio
    async def test_fresh_index_zero_blob_reads(self, vfs_nts):
        """FreshIndexCompleteNoBlobReads: all fresh artifacts → results complete, zero blob reads."""
        ns, agent = await _setup_vfs(vfs_nts)
        await vfs_nts.write(ns.id, "/a.txt", b"unique phrase alpha", principal_id=agent.id)
        await vfs_nts.write(ns.id, "/b.txt", b"unique phrase beta", principal_id=agent.id)
        await vfs_nts.write(ns.id, "/c.txt", b"something else entirely", principal_id=agent.id)

        counting = _CountingBlobStore(vfs_nts._blob)
        vfs_nts._blob = counting

        results = await vfs_nts.search(ns.id, "unique phrase", "/", SearchType.REGEX, principal_id=agent.id)

        assert counting.get_count == 0, "fresh index must serve regex with zero blob reads"
        assert {r.path for r in results} == {"/a.txt", "/b.txt"}

    @pytest.mark.asyncio
    async def test_fresh_index_fulltext_zero_blob_reads(self, vfs_nts):
        """FreshIndexCompleteNoBlobReads: fulltext search over fresh index uses zero blob reads."""
        ns, agent = await _setup_vfs(vfs_nts)
        await vfs_nts.write(ns.id, "/a.txt", b"galaxy clusters", principal_id=agent.id)
        await vfs_nts.write(ns.id, "/b.txt", b"stellar winds", principal_id=agent.id)

        counting = _CountingBlobStore(vfs_nts._blob)
        vfs_nts._blob = counting

        results = await vfs_nts.search(ns.id, "galaxy", "/", SearchType.FULLTEXT, principal_id=agent.id)

        assert counting.get_count == 0, "fresh index must serve fulltext with zero blob reads"
        assert {r.path for r in results} == {"/a.txt"}


# ---------------------------------------------------------------------------
# ColdIndexFailsLoud/AnyStragglerFailsLoud
# ---------------------------------------------------------------------------


class TestAnyStragglerFailsLoud:
    """AnyStragglerFailsLoud: any in-scope straggler fails the search loud — no verify, no reads."""

    @pytest.mark.asyncio
    async def test_single_straggler_fails_loud_regex(self, vfs_nts):
        """A single missing-artifact straggler raises ReindexRequiredError with zero blob reads."""
        ns, agent = await _setup_vfs(vfs_nts)
        await vfs_nts.write(ns.id, "/indexed.txt", b"indexed phrase here", principal_id=agent.id)
        await vfs_nts.write(ns.id, "/straggler.txt", b"straggler phrase here", principal_id=agent.id)
        await _clear_search_meta(vfs_nts, ns.id, "/straggler.txt")

        counting = _CountingBlobStore(vfs_nts._blob)
        vfs_nts._blob = counting

        with pytest.raises(ReindexRequiredError, match="reindex"):
            await vfs_nts.search(ns.id, "phrase", "/", SearchType.REGEX, principal_id=agent.id)
        assert counting.get_count == 0, "fail-loud must not read blobs"

    @pytest.mark.asyncio
    async def test_single_straggler_fails_loud_fulltext(self, vfs_nts):
        """A FULLTEXT search with any straggler fails loud — no inline approximation."""
        ns, agent = await _setup_vfs(vfs_nts)
        await vfs_nts.write(ns.id, "/indexed.txt", b"galaxy clusters", principal_id=agent.id)
        await vfs_nts.write(ns.id, "/straggler.txt", b"galaxy winds", principal_id=agent.id)
        await _clear_search_meta(vfs_nts, ns.id, "/straggler.txt")

        counting = _CountingBlobStore(vfs_nts._blob)
        vfs_nts._blob = counting

        with pytest.raises(ReindexRequiredError, match="reindex"):
            await vfs_nts.search(ns.id, "galaxy", "/", SearchType.FULLTEXT, principal_id=agent.id)
        assert counting.get_count == 0, "fail-loud must not read blobs"

    @pytest.mark.asyncio
    async def test_stale_content_hash_is_straggler(self, vfs_nts):
        """A ready artifact with a drifted content_hash is a straggler → fail loud."""
        ns, agent = await _setup_vfs(vfs_nts)
        ver = await vfs_nts.write(ns.id, "/stale.txt", b"fresh content here", principal_id=agent.id)

        nts = vfs_nts._meta.native_text_search()
        stale_artifact = SearchArtifact(
            status="ready",
            schema_version=1,
            provider_key=nts.provider_key,
            provider_version="1",
            params_hash=nts.params_hash,
            content_hash="stale_hash_that_does_not_match",
            created_at=ver.created_at,
            storage="external",
            artifact_ref=f"{nts.provider_key}:{nts.params_hash}:stale_hash_that_does_not_match",
        )
        await vfs_nts._meta.update_search_artifact(ver.id, nts.provider_key, stale_artifact)

        with pytest.raises(ReindexRequiredError, match="reindex"):
            await vfs_nts.search(ns.id, "fresh content", "/", SearchType.REGEX, principal_id=agent.id)


class TestDecidedNonMatchExcluded:
    """DecidedNonMatchExcluded: identity-current unsupported/failed artifacts are excluded, not stragglers."""

    @pytest.mark.asyncio
    async def test_identity_current_failed_excluded_not_failloud(self, vfs_nts):
        """An identity-current `failed` artifact is a decided non-match — excluded, no fail-loud, no reads."""
        ns, agent = await _setup_vfs(vfs_nts)
        await vfs_nts.write(ns.id, "/match.txt", b"target word inside", principal_id=agent.id)
        ver = await vfs_nts.write(ns.id, "/failed.txt", b"target word also here", principal_id=agent.id)

        # Mark /failed.txt with an identity-current 'failed' artifact (e.g. oversized at index time).
        nts = vfs_nts._meta.native_text_search()
        failed_artifact = SearchArtifact(
            status="failed",
            schema_version=1,
            provider_key=nts.provider_key,
            provider_version="1",
            params_hash=nts.params_hash,
            content_hash=ver.content_hash,
            created_at=ver.created_at,
            storage="inline",
            error_code="too_large",
            error_message="content exceeds index size limit",
        )
        await vfs_nts._meta.update_search_artifact(ver.id, nts.provider_key, failed_artifact)

        counting = _CountingBlobStore(vfs_nts._blob)
        vfs_nts._blob = counting

        results = await vfs_nts.search(ns.id, "target", "/", SearchType.REGEX, principal_id=agent.id)

        assert counting.get_count == 0, "decided non-match must not read blobs and must not fail loud"
        assert {r.path for r in results} == {"/match.txt"}, "failed-artifact file is excluded, not a straggler"


# ---------------------------------------------------------------------------
# ColdIndexFailsLoud/ColdIndexFailsLoud
# ---------------------------------------------------------------------------


class TestColdIndexFailsLoud:
    @pytest.mark.asyncio
    async def test_index_store_error_raises_index_unavailable(self, vfs_nts):
        """ColdIndexFailsLoud: index store error during search raises IndexUnavailableError."""
        ns, agent = await _setup_vfs(vfs_nts)
        await vfs_nts.write(ns.id, "/a.txt", b"test content", principal_id=agent.id)

        # Replace search_text with one that raises an infrastructure error.
        nts = vfs_nts._meta.native_text_search()
        nts.search_text = AsyncMock(side_effect=RuntimeError("DB connection lost"))

        with pytest.raises(IndexUnavailableError, match="reindex"):
            await vfs_nts.search(ns.id, "test", "/", SearchType.REGEX, principal_id=agent.id)

    @pytest.mark.asyncio
    async def test_any_straggler_raises_reindex_required(self, vfs_nts):
        """ColdIndexFailsLoud: a single straggler (not a budget overflow) raises ReindexRequiredError."""
        ns, agent = await _setup_vfs(vfs_nts)
        await vfs_nts.write(ns.id, "/a.txt", b"content here", principal_id=agent.id)
        await _clear_search_meta(vfs_nts, ns.id, "/a.txt")

        with pytest.raises(ReindexRequiredError, match="reindex"):
            await vfs_nts.search(ns.id, "content", "/", SearchType.REGEX, principal_id=agent.id)

    @pytest.mark.asyncio
    async def test_reindex_required_message_names_scope(self, vfs_nts):
        """ColdIndexFailsLoud: the error names a path-scoped reindex over the search scope, with a count."""
        ns, agent = await _setup_vfs(vfs_nts)
        await vfs_nts.write(ns.id, "/sub/a.txt", b"x", principal_id=agent.id)
        await vfs_nts.write(ns.id, "/sub/b.txt", b"x", principal_id=agent.id)
        await _clear_search_meta(vfs_nts, ns.id, "/sub/a.txt")
        await _clear_search_meta(vfs_nts, ns.id, "/sub/b.txt")

        with pytest.raises(ReindexRequiredError) as exc_info:
            await vfs_nts.search(ns.id, "x", "/sub/", SearchType.REGEX, principal_id=agent.id)

        msg = str(exc_info.value)
        assert "/sub/" in msg, "message must name the search scope for a path-scoped reindex"
        assert "2 file" in msg, "message includes the straggler count"
        assert "reindex" in msg


# ---------------------------------------------------------------------------
# ColdIndexFailsLoud/UndecodableContentIsUnsupported
# ---------------------------------------------------------------------------


class TestBinaryUnsupportedSkipsBudget:
    """B2 regression: identity-matched 'unsupported' artifacts do not consume straggler budget."""

    @pytest.mark.asyncio
    async def test_binary_files_beyond_budget_do_not_raise_after_reindex(self, vfs_nts):
        """B2: 11+ binary files + budget 10 → reindex → regex search succeeds with text results.

        Before the fix, every binary file (status='unsupported') was classified as a
        straggler.  With >max_content_reads binary files in scope, every search raised
        ReindexRequiredError even immediately after reindex.
        """
        ns, agent = await _setup_vfs(vfs_nts)

        budget = SearchLimits().max_content_reads  # default 10
        # Write budget+1 binary files (each will have an 'unsupported' artifact after reindex).
        for i in range(budget + 1):
            await vfs_nts.write(ns.id, f"/binary{i:02d}.bin", b"\xff\xfe\x00binary", principal_id=agent.id)
        # Write one text file that should appear in results.
        await vfs_nts.write(ns.id, "/text.txt", b"findable text here", principal_id=agent.id)

        # After write, binary files already have 'unsupported' artifacts; reindex refreshes them.
        await vfs_nts.reindex(ns.id)

        # Search must succeed — binary files are confirmed non-matches, not stragglers.
        results = await vfs_nts.search(ns.id, "findable", "/", SearchType.REGEX, principal_id=agent.id)
        assert {r.path for r in results} == {"/text.txt"}

    @pytest.mark.asyncio
    async def test_binary_straggler_with_no_artifact_fails_loud(self, vfs_nts):
        """A binary file whose artifact was cleared is a straggler (absent artifact) → fail loud.

        An absent artifact is indistinguishable from any other straggler; the search fails loud
        rather than reading and self-healing. ``reindex`` then records the identity-current
        ``unsupported`` artifact that makes the file a decided non-match thereafter.
        """
        ns, agent = await _setup_vfs(vfs_nts)

        await vfs_nts.write(ns.id, "/binary.bin", b"\xff\xfe\x00binary", principal_id=agent.id)
        await vfs_nts.write(ns.id, "/text.txt", b"hello world", principal_id=agent.id)
        await _clear_search_meta(vfs_nts, ns.id, "/binary.bin")

        with pytest.raises(ReindexRequiredError, match="reindex"):
            await vfs_nts.search(ns.id, "hello", "/", SearchType.REGEX, principal_id=agent.id)

        # reindex records the unsupported artifact; the binary file is then a decided non-match.
        await vfs_nts.reindex(ns.id)
        results = await vfs_nts.search(ns.id, "hello", "/", SearchType.REGEX, principal_id=agent.id)
        assert {r.path for r in results} == {"/text.txt"}


# NOTE: the former S2 "external record missing → reclassified as straggler" defense is removed.
# The native path no longer re-checks record existence per query; instead blob GC is made atomic
# (NativeTextSearchStorage/LiveReferencedContentNeverSwept), so a live-referenced content_hash is
# never swept and an identity-current 'ready' artifact always has its record present.


class TestUndecodableContentIsUnsupported:
    @pytest.mark.asyncio
    async def test_binary_content_produces_unsupported_artifact(self, vfs_nts):
        """UndecodableContentIsUnsupported: non-UTF-8 write succeeds with 'unsupported' artifact."""
        ns, agent = await _setup_vfs(vfs_nts)

        # Write binary (non-UTF-8) content.
        binary_content = b"\xff\xfe\x00\x01\x80\x81\x82"
        ver = await vfs_nts.write(ns.id, "/binary.bin", binary_content, principal_id=agent.id)

        # Write must succeed and return a VersionMeta.
        assert ver is not None
        assert ver.content_hash != ""

        # search_meta must contain an 'unsupported' artifact for the NTS provider.
        nts = vfs_nts._meta.native_text_search()
        assert nts.provider_key in ver.search_meta, "NTS provider key absent from search_meta"
        artifact = SearchArtifact.from_dict(ver.search_meta[nts.provider_key])
        assert artifact.status == "unsupported"
        assert artifact.error_code == "decode_error"

    @pytest.mark.asyncio
    async def test_binary_file_is_decided_non_match(self, vfs_nts):
        """A freshly-written binary file is a decided non-match (identity-current 'unsupported').

        Its artifact is excluded from results without a blob read and does NOT trigger fail-loud —
        binary content cannot satisfy a text predicate.
        """
        ns, agent = await _setup_vfs(vfs_nts)
        await vfs_nts.write(ns.id, "/text.txt", b"searchable text", principal_id=agent.id)
        await vfs_nts.write(ns.id, "/binary.bin", b"\xff\xfe\x00binary", principal_id=agent.id)

        results = await vfs_nts.search(ns.id, "searchable", "/", SearchType.REGEX, principal_id=agent.id)

        # Only the text file matches; the binary file is a decided non-match, excluded (not errored).
        assert {r.path for r in results} == {"/text.txt"}


# ---------------------------------------------------------------------------
# SearchMetaReindex/BatchReindex
# ---------------------------------------------------------------------------


class TestBatchReindex:
    @pytest.mark.asyncio
    async def test_reindex_produces_ready_artifacts(self, vfs_nts):
        """BatchReindex: reindex writes ready external artifacts for all in-scope files."""
        ns, agent = await _setup_vfs(vfs_nts)
        paths = ["/a.txt", "/b.txt", "/c.txt"]
        for path in paths:
            await vfs_nts.write(ns.id, path, f"content for {path}".encode(), principal_id=agent.id)
            await _clear_search_meta(vfs_nts, ns.id, path)  # simulate un-indexed state

        count = await vfs_nts.reindex(ns.id)

        assert count == len(paths), f"expected {len(paths)} updated, got {count}"

        nts = vfs_nts._meta.native_text_search()
        for path in paths:
            ver = await vfs_nts._meta.get_version(ns.id, path)
            assert nts.provider_key in ver.search_meta, f"{path}: NTS key absent after reindex"
            artifact = SearchArtifact.from_dict(ver.search_meta[nts.provider_key])
            assert artifact.status == "ready", f"{path}: expected ready artifact, got {artifact.status}"
            assert artifact.storage == "external"

    @pytest.mark.asyncio
    async def test_reindex_is_searchable_after(self, vfs_nts):
        """BatchReindex: after reindex, search returns matches with zero blob reads."""
        ns, agent = await _setup_vfs(vfs_nts)
        await vfs_nts.write(ns.id, "/doc.txt", b"reindexed content here", principal_id=agent.id)
        await _clear_search_meta(vfs_nts, ns.id, "/doc.txt")

        await vfs_nts.reindex(ns.id)

        counting = _CountingBlobStore(vfs_nts._blob)
        vfs_nts._blob = counting

        results = await vfs_nts.search(ns.id, "reindexed", "/", SearchType.REGEX, principal_id=agent.id)

        assert counting.get_count == 0, "after reindex, search must use zero blob reads"
        assert {r.path for r in results} == {"/doc.txt"}

    @pytest.mark.asyncio
    async def test_reindex_binary_file_produces_unsupported_artifact(self, vfs_nts):
        """BatchReindex: binary files receive an 'unsupported' artifact (not an error)."""
        ns, agent = await _setup_vfs(vfs_nts)
        await vfs_nts.write(ns.id, "/binary.bin", b"\xff\xfe\x00binary", principal_id=agent.id)
        await _clear_search_meta(vfs_nts, ns.id, "/binary.bin")

        count = await vfs_nts.reindex(ns.id)

        assert count == 1
        nts = vfs_nts._meta.native_text_search()
        ver = await vfs_nts._meta.get_version(ns.id, "/binary.bin")
        artifact = SearchArtifact.from_dict(ver.search_meta[nts.provider_key])
        assert artifact.status == "unsupported"

    @pytest.mark.asyncio
    async def test_reindex_scope_limits_backfill(self, vfs_nts):
        """BatchReindex: scope parameter limits which files are reindexed."""
        ns, agent = await _setup_vfs(vfs_nts)
        await vfs_nts.write(ns.id, "/src/a.txt", b"in scope", principal_id=agent.id)
        await vfs_nts.write(ns.id, "/other/b.txt", b"out of scope", principal_id=agent.id)
        await _clear_search_meta(vfs_nts, ns.id, "/src/a.txt")
        await _clear_search_meta(vfs_nts, ns.id, "/other/b.txt")

        count = await vfs_nts.reindex(ns.id, scope="/src/")

        assert count == 1

        nts = vfs_nts._meta.native_text_search()
        ver_in = await vfs_nts._meta.get_version(ns.id, "/src/a.txt")
        ver_out = await vfs_nts._meta.get_version(ns.id, "/other/b.txt")
        assert nts.provider_key in ver_in.search_meta, "/src/a.txt should be reindexed"
        assert nts.provider_key not in ver_out.search_meta, "/other/b.txt must not be reindexed"


# ---------------------------------------------------------------------------
# SearchMetaReindex/SearchPerformsNoLazyBackfill
# ---------------------------------------------------------------------------


class TestSearchPerformsNoLazyBackfill:
    """SearchPerformsNoLazyBackfill: a fail-loud search writes nothing; reindex is the sole remedy."""

    @pytest.mark.asyncio
    async def test_failloud_writes_nothing_and_reindex_is_the_remedy(self, vfs_nts):
        ns, agent = await _setup_vfs(vfs_nts)
        await vfs_nts.write(ns.id, "/doc.txt", b"recoverable content", principal_id=agent.id)
        await _clear_search_meta(vfs_nts, ns.id, "/doc.txt")

        # First search fails loud and must NOT write an artifact (no lazy backfill).
        with pytest.raises(ReindexRequiredError):
            await vfs_nts.search(ns.id, "recoverable", "/", SearchType.REGEX, principal_id=agent.id)

        nts = vfs_nts._meta.native_text_search()
        ver = await vfs_nts._meta.get_version(ns.id, "/doc.txt")
        assert nts.provider_key not in ver.search_meta, "fail-loud search must not backfill an artifact"

        # A second search before reindex still fails loud (no self-healing happened).
        with pytest.raises(ReindexRequiredError):
            await vfs_nts.search(ns.id, "recoverable", "/", SearchType.REGEX, principal_id=agent.id)

        # reindex is the remedy — afterward the search succeeds with zero blob reads.
        await vfs_nts.reindex(ns.id)
        counting = _CountingBlobStore(vfs_nts._blob)
        vfs_nts._blob = counting
        results = await vfs_nts.search(ns.id, "recoverable", "/", SearchType.REGEX, principal_id=agent.id)
        assert counting.get_count == 0
        assert {r.path for r in results} == {"/doc.txt"}


# ---------------------------------------------------------------------------
# SearchMetaReindex/RollbackCopiesSearchMeta
# ---------------------------------------------------------------------------


class TestRollbackCopiesSearchMeta:
    @pytest.mark.asyncio
    async def test_rollback_copies_search_meta(self, vfs_nts):
        """RollbackCopiesSearchMeta: rollback version inherits search_meta from target version."""
        ns, agent = await _setup_vfs(vfs_nts)

        v1 = await vfs_nts.write(ns.id, "/file.txt", b"original content", principal_id=agent.id)
        # v2: different content so v1 is superseded.
        await vfs_nts.write(ns.id, "/file.txt", b"different content", principal_id=agent.id)
        # v3: rollback to v1.
        v3 = await vfs_nts.rollback(ns.id, "/file.txt", 1, principal_id=agent.id)

        # v3 must carry v1's search_meta (same content_hash → same artifact).
        assert v3.search_meta == v1.search_meta, "rollback must copy search_meta from target version"

        nts = vfs_nts._meta.native_text_search()
        assert nts.provider_key in v3.search_meta

    @pytest.mark.asyncio
    async def test_rollback_artifact_resolves_without_reindex(self, vfs_nts):
        """RollbackCopiesSearchMeta: rolled-back version's artifact resolves; search uses zero reads."""
        ns, agent = await _setup_vfs(vfs_nts)

        await vfs_nts.write(ns.id, "/file.txt", b"searchable rollback content", principal_id=agent.id)
        await vfs_nts.write(ns.id, "/file.txt", b"interim content", principal_id=agent.id)
        await vfs_nts.rollback(ns.id, "/file.txt", 1, principal_id=agent.id)

        counting = _CountingBlobStore(vfs_nts._blob)
        vfs_nts._blob = counting

        results = await vfs_nts.search(ns.id, "searchable rollback", "/", SearchType.REGEX, principal_id=agent.id)

        assert counting.get_count == 0, "rollback reuses content-addressed artifact; no blob read needed"
        assert {r.path for r in results} == {"/file.txt"}

    @pytest.mark.asyncio
    async def test_rollback_to_binary_version_keeps_unsupported_artifact(self, vfs_nts):
        """RollbackCopiesSearchMeta: rolling back to a binary version copies its unsupported artifact."""
        ns, agent = await _setup_vfs(vfs_nts)

        # v1: binary content → unsupported artifact.
        v1 = await vfs_nts.write(ns.id, "/file.bin", b"\xff\xfe binary", principal_id=agent.id)
        # v2: text content → ready artifact.
        await vfs_nts.write(ns.id, "/file.bin", b"text content", principal_id=agent.id)
        # v3: rollback to v1 (binary).
        v3 = await vfs_nts.rollback(ns.id, "/file.bin", 1, principal_id=agent.id)

        nts = vfs_nts._meta.native_text_search()
        assert v3.search_meta == v1.search_meta
        artifact = SearchArtifact.from_dict(v3.search_meta[nts.provider_key])
        assert artifact.status == "unsupported"
