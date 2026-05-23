"""Tests for VFS URI resolution and lifecycle."""

from __future__ import annotations

import importlib.metadata
import importlib.util

import pytest
import pytest_asyncio

from vfs.config import VFSConfig
from vfs.stores.cached_blob import CachedBlobStore
from vfs.stores.local_blob import LocalFSBlobStore
from vfs.stores.sqlite_metadata import SQLiteMetadataStore
from vfs.vfs import _BLOB_OPTIONAL, _METADATA_OPTIONAL, VFS, _load_optional_adapter

_ALL_OPTIONAL = {**_METADATA_OPTIONAL, **_BLOB_OPTIONAL}


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


class TestOptionalAdapterResolution:
    """URIBasedStoreResolution / MissingExtraRaises: the new schemes resolve to optional
    adapters and raise an actionable, dependency-naming error when the extra is absent."""

    @pytest.mark.parametrize(
        ("uri_key", "uri", "extra", "driver"),
        [
            ("metadata_store_uri", "postgresql://localhost/aifs", "postgres", "asyncpg"),
            ("metadata_store_uri", "mongodb://localhost/aifs", "mongo", "motor"),
            ("blob_store_uri", "s3://my-bucket/aifs", "s3", "aiobotocore"),
        ],
    )
    def test_missing_extra_raises_naming_dependency(self, tmp_path, uri_key, uri, extra, driver):
        if importlib.util.find_spec(driver) is not None:
            pytest.skip(f"{driver} is installed; the missing-extra path is not exercised")
        kwargs = {
            "metadata_store_uri": f"sqlite:///{tmp_path}/test.db",
            "blob_store_uri": f"file:///{tmp_path}/blobs/",
            uri_key: uri,
        }
        config = VFSConfig(**kwargs)
        with pytest.raises(ImportError) as excinfo:
            VFS(config)
        message = str(excinfo.value)
        assert driver in message  # names the missing optional dependency
        assert extra in message  # names the installable extra

    def test_unknown_scheme_still_raises_value_error(self, tmp_path):
        """A genuinely unknown scheme remains a ValueError, distinct from a missing extra."""
        config = VFSConfig(
            metadata_store_uri="redis://localhost",
            blob_store_uri=f"file:///{tmp_path}/blobs/",
        )
        with pytest.raises(ValueError, match="Unsupported metadata URI"):
            VFS(config)

    @pytest.mark.parametrize("scheme", sorted(_ALL_OPTIONAL))
    def test_optional_extra_is_declared_and_names_its_driver(self, scheme):
        """Each scheme the resolver points at via 'pip install ai-vfs[extra]' is a real,
        installable extra that requires the named driver — so the error's remediation works."""
        extra, driver, _adapter_module, _class_name = _ALL_OPTIONAL[scheme]
        provides_extra = importlib.metadata.metadata("ai-vfs").get_all("Provides-Extra") or []
        assert extra in provides_extra, f"extra {extra!r} is not declared in package metadata"
        requirements = importlib.metadata.requires("ai-vfs") or []
        gated = [r for r in requirements if f"extra == '{extra}'" in r or f'extra == "{extra}"' in r]
        assert any(driver in req for req in gated), f"extra {extra!r} does not require {driver!r}"

    def test_optional_adapter_missing_module_raises_clear_error(self):
        """When the driver is present but the adapter module is not yet shipped, the error
        is an actionable 'not available' message rather than a raw ModuleNotFoundError."""
        # 'json' stands in for an installed driver so the driver check passes.
        spec = ("fake", "json", "vfs.stores.does_not_exist", "Missing")
        with pytest.raises(ImportError, match="not available in this build"):
            _load_optional_adapter("fake://", spec)


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
