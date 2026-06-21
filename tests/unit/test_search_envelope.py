"""Tests for SearchArtifact envelope and DefaultSearchProvider protocol migration.

Task group: Envelope & Protocol Foundation
Covers: SearchArtifactEnvelope/* (usability, round-trip, external record),
        SearchProviderProtocol/* (DefaultProvider migrated, index returns None),
        SearchMetadataExtensible/ManifestReferencesExternalTextRecord.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from vfs.models import FileMeta, SearchArtifact, SearchType
from vfs.protocols.search import (
    SearchMetaEntry,
    SearchRequest,
    SearchResponse,
)
from vfs.search.default import DefaultSearchProvider
from vfs.search.reader import ContentReader

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _artifact(
    *,
    status: str = "ready",
    content_hash: str = "abcdef",
    params_hash: str = "params1",
    storage: str = "inline",
    artifact_ref: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> SearchArtifact:
    return SearchArtifact(
        status=status,
        schema_version=1,
        provider_key="test-provider",
        provider_version="0.1.0",
        params_hash=params_hash,
        content_hash=content_hash,
        created_at=_now(),
        storage=storage,
        artifact_ref=artifact_ref,
        error_code=error_code,
        error_message=error_message,
    )


class _NullBlob:
    """Blob store stub that returns empty bytes for every hash."""

    async def get(self, ch: str) -> bytes:
        return b""


def _entry(path: str) -> SearchMetaEntry:
    return SearchMetaEntry(
        version_id=f"ver-{path}",
        path=path,
        content_hash="00",
        size=0,
        updated_at=_now(),
    )


# ---------------------------------------------------------------------------
# SearchArtifact usability helper (SearchArtifactEnvelope/*)
# ---------------------------------------------------------------------------


class TestSearchArtifactUsability:
    def test_ready_matching_is_usable(self):
        """ReadyArtifactUsable: ready + matching hashes → is_usable() is True."""
        art = _artifact(status="ready", content_hash="abc", params_hash="p1")
        assert art.is_usable(current_content_hash="abc", active_params_hash="p1") is True

    def test_content_hash_mismatch_is_stale(self):
        """ContentHashMismatchIsStale: ready but wrong content_hash → is_usable() is False."""
        art = _artifact(status="ready", content_hash="old", params_hash="p1")
        assert art.is_usable(current_content_hash="new", active_params_hash="p1") is False

    def test_params_hash_mismatch_is_stale(self):
        """ParamsHashMismatchIsStale: ready but wrong params_hash → is_usable() is False."""
        art = _artifact(status="ready", content_hash="abc", params_hash="old-params")
        assert art.is_usable(current_content_hash="abc", active_params_hash="new-params") is False

    def test_failed_status_is_not_usable(self):
        art = _artifact(status="failed")
        assert art.is_usable(current_content_hash="abcdef", active_params_hash="params1") is False

    def test_unsupported_status_is_not_usable(self):
        art = _artifact(status="unsupported")
        assert art.is_usable(current_content_hash="abcdef", active_params_hash="params1") is False

    def test_external_ready_matching_is_usable(self):
        """An external `ready` artifact with matching hashes is usable.

        The former external-record readability/identity check (and its `external_readable` /
        `external_identity_match` params) is removed: the text record is content-addressed and
        resident in the metadata store, so an identity-current artifact's record is always present
        (blob GC never sweeps a live-referenced content_hash).
        """
        art = _artifact(status="ready", storage="external", artifact_ref="sha256:abc")
        assert art.is_usable(current_content_hash="abcdef", active_params_hash="params1") is True


# ---------------------------------------------------------------------------
# SearchArtifact round-trip serialization
# ---------------------------------------------------------------------------


class TestSearchArtifactRoundTrip:
    def test_all_fields_survive_round_trip(self):
        """All envelope fields must survive to_dict() → from_dict() unchanged."""
        now = datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        art = SearchArtifact(
            status="failed",
            schema_version=2,
            provider_key="fts-sqlite",
            provider_version="1.2.3",
            params_hash="ph-xyz",
            content_hash="ch-xyz",
            created_at=now,
            storage="blob",
            payload={"raw_text": "hello"},
            artifact_ref="ref-1",
            error_code="DECODE_ERROR",
            error_message="UTF-8 decode failed",
        )
        restored = SearchArtifact.from_dict(art.to_dict())
        assert restored == art

    def test_created_at_serialized_as_iso_string(self):
        """to_dict() stores created_at as an ISO-8601 string, not a datetime object."""
        now = datetime(2025, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
        art = SearchArtifact(
            status="ready",
            schema_version=1,
            provider_key="p",
            provider_version="0.1",
            params_hash="ph",
            content_hash="ch",
            created_at=now,
            storage="inline",
        )
        d = art.to_dict()
        assert isinstance(d["created_at"], str), "created_at must be a string in the serialized dict"
        assert "T" in d["created_at"], "created_at must be ISO-8601 (contains 'T')"

    def test_from_dict_accepts_preparsed_datetime(self):
        """from_dict() is idempotent: it accepts an already-parsed datetime for created_at."""
        now = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        art = _artifact()
        d = art.to_dict()
        d["created_at"] = now  # pre-parsed
        restored = SearchArtifact.from_dict(d)
        assert restored.created_at == now

    def test_error_fields_round_trip(self):
        """error_code and error_message survive serialization."""
        art = _artifact(
            status="failed",
            error_code="DECODE_ERROR",
            error_message="invalid UTF-8 at offset 42",
        )
        restored = SearchArtifact.from_dict(art.to_dict())
        assert restored.error_code == "DECODE_ERROR"
        assert restored.error_message == "invalid UTF-8 at offset 42"

    def test_none_optional_fields_survive(self):
        """Optional fields default to None and survive round-trip as None."""
        art = SearchArtifact(
            status="ready",
            schema_version=1,
            provider_key="p",
            provider_version="0.1",
            params_hash="ph",
            content_hash="ch",
            created_at=_now(),
            storage="inline",
        )
        restored = SearchArtifact.from_dict(art.to_dict())
        assert restored.payload is None
        assert restored.artifact_ref is None
        assert restored.error_code is None
        assert restored.error_message is None

    def test_external_artifact_in_search_meta_manifest(self):
        """ManifestReferencesExternalTextRecord: external artifact round-trips via search_meta dict."""
        art = SearchArtifact(
            status="ready",
            schema_version=1,
            provider_key="fts-sqlite",
            provider_version="0.1.0",
            params_hash="ph1",
            content_hash="ch1",
            created_at=_now(),
            storage="external",
            artifact_ref="sha256:abcdef0123456789",
        )
        # Simulate how search_meta is stored: {provider_key: artifact.to_dict()}
        manifest: dict[str, Any] = {art.provider_key: art.to_dict()}

        restored = SearchArtifact.from_dict(manifest["fts-sqlite"])
        assert restored.storage == "external"
        assert restored.artifact_ref == "sha256:abcdef0123456789"
        assert restored.provider_key == "fts-sqlite"
        assert restored.status == "ready"


# ---------------------------------------------------------------------------
# DefaultSearchProvider protocol migration (SearchProviderProtocol/*)
# ---------------------------------------------------------------------------


class TestDefaultProviderProtocol:
    @pytest.mark.asyncio
    async def test_index_returns_none(self):
        """IndexReturnsArtifactOrNone: DefaultSearchProvider.index() returns None."""
        provider = DefaultSearchProvider()
        now = _now()
        meta = FileMeta(
            namespace_id="ns",
            path="/a.py",
            current_version_id="v1",
            current_version_number=1,
            created_at=now,
            updated_at=now,
        )
        result = await provider.index("/a.py", b"content", meta)
        assert result is None

    @pytest.mark.asyncio
    async def test_glob_search_via_request_returns_response(self):
        """DefaultProviderMigratedToRequest: glob uses SearchRequest; result is SearchResponse."""
        provider = DefaultSearchProvider()
        entries = [_entry("/src/a.py"), _entry("/src/b.txt")]
        reader = ContentReader(entries=entries, blob=_NullBlob(), max_reads=0)
        req = SearchRequest(
            query="*.py",
            scope="/src/",
            search_type=SearchType.GLOB,
            search_metas=entries,
            read_content=reader,
        )
        resp = await provider.search(req)
        assert isinstance(resp, SearchResponse)
        assert {r.path for r in resp.results} == {"/src/a.py"}

    @pytest.mark.asyncio
    async def test_find_search_via_request_returns_response(self):
        """DefaultProviderMigratedToRequest: find uses SearchRequest; result is SearchResponse."""
        provider = DefaultSearchProvider()
        entries = [_entry("/a.py"), _entry("/b.txt")]
        reader = ContentReader(entries=entries, blob=_NullBlob(), max_reads=0)
        req = SearchRequest(
            query="*.py",
            scope="/",
            search_type=SearchType.FIND,
            search_metas=entries,
            read_content=reader,
        )
        resp = await provider.search(req)
        assert isinstance(resp, SearchResponse)
        assert {r.path for r in resp.results} == {"/a.py"}
