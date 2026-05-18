"""Tests for VFS URI resolution and lifecycle."""

from __future__ import annotations

import pytest
import pytest_asyncio

from vfs.config import VFSConfig
from vfs.stores.cached_blob import CachedBlobStore
from vfs.stores.local_blob import LocalFSBlobStore
from vfs.stores.sqlite_metadata import SQLiteMetadataStore
from vfs.vfs import VFS


class TestVFSURIResolution:
    """Task 13: VFS structure and URI resolution."""

    def test_sqlite_uri_resolves(self, tmp_path):
        config = VFSConfig(
            metadata_store_uri=f"sqlite:///{tmp_path}/test.db",
            blob_store_uri=f"file:///{tmp_path}/blobs/",
        )
        vfs = VFS(config)
        assert isinstance(vfs._meta, SQLiteMetadataStore)

    def test_file_uri_resolves(self, tmp_path):
        config = VFSConfig(
            metadata_store_uri=f"sqlite:///{tmp_path}/test.db",
            blob_store_uri=f"file:///{tmp_path}/blobs/",
        )
        vfs = VFS(config)
        assert isinstance(vfs._blob, LocalFSBlobStore)

    def test_cache_disabled_for_local_fs(self, tmp_path):
        config = VFSConfig(
            metadata_store_uri=f"sqlite:///{tmp_path}/test.db",
            blob_store_uri=f"file:///{tmp_path}/blobs/",
            blob_cache_enabled=None,
        )
        vfs = VFS(config)
        assert isinstance(vfs._blob, LocalFSBlobStore)
        assert not isinstance(vfs._blob, CachedBlobStore)

    def test_cache_enabled_explicitly(self, tmp_path):
        config = VFSConfig(
            metadata_store_uri=f"sqlite:///{tmp_path}/test.db",
            blob_store_uri=f"file:///{tmp_path}/blobs/",
            blob_cache_enabled=True,
        )
        vfs = VFS(config)
        assert isinstance(vfs._blob, CachedBlobStore)

    def test_unknown_metadata_uri_raises(self, tmp_path):
        config = VFSConfig(
            metadata_store_uri="badscheme://whatever",
            blob_store_uri=f"file:///{tmp_path}/blobs/",
        )
        with pytest.raises(ValueError, match="Unsupported metadata URI"):
            VFS(config)

    def test_unknown_blob_uri_raises(self, tmp_path):
        config = VFSConfig(
            metadata_store_uri=f"sqlite:///{tmp_path}/test.db",
            blob_store_uri="badscheme://whatever",
        )
        with pytest.raises(ValueError, match="Unsupported blob URI"):
            VFS(config)


class TestProcessIdentification:
    """ProcessIdentification (design D11): VFS.initialize sets the process title when running as a service."""

    @pytest.mark.asyncio
    async def test_initialize_sets_process_title(self, tmp_path, monkeypatch):
        import setproctitle

        captured: list[str] = []
        monkeypatch.setattr(setproctitle, "setproctitle", lambda t: captured.append(t))
        config = VFSConfig(
            metadata_store_uri=f"sqlite:///{tmp_path}/test.db",
            blob_store_uri=f"file:///{tmp_path}/blobs/",
            otel_enabled=False,
        )
        vfs = VFS(config)
        try:
            await vfs.initialize(set_proc_title=True)
        finally:
            await vfs.close()
        assert "ai-vfs: service" in captured

    @pytest.mark.asyncio
    async def test_initialize_default_does_not_set_process_title(self, tmp_path, monkeypatch):
        import setproctitle

        captured: list[str] = []
        monkeypatch.setattr(setproctitle, "setproctitle", lambda t: captured.append(t))
        config = VFSConfig(
            metadata_store_uri=f"sqlite:///{tmp_path}/test.db",
            blob_store_uri=f"file:///{tmp_path}/blobs/",
            otel_enabled=False,
        )
        vfs = VFS(config)
        try:
            await vfs.initialize()  # default set_proc_title=False
        finally:
            await vfs.close()
        assert captured == []
