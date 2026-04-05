"""Tests for VFSConfig."""

from __future__ import annotations

import pytest


class TestVFSConfigDefaults:
    """Default values match spec."""

    def test_metadata_store_uri_default(self):
        from vfs.config import VFSConfig

        config = VFSConfig()
        assert config.metadata_store_uri == "sqlite:///./aifs.db"

    def test_blob_store_uri_default(self):
        from vfs.config import VFSConfig

        config = VFSConfig()
        assert config.blob_store_uri == "file:///./aifs_blobs/"

    def test_otel_enabled_default(self):
        from vfs.config import VFSConfig

        config = VFSConfig()
        assert config.otel_enabled is True

    def test_audit_log_enabled_default(self):
        from vfs.config import VFSConfig

        config = VFSConfig()
        assert config.audit_log_enabled is True

    def test_search_providers_default(self):
        from vfs.config import VFSConfig

        config = VFSConfig()
        assert config.search_providers == ["default"]

    def test_blob_cache_enabled_default_none(self):
        from vfs.config import VFSConfig

        config = VFSConfig()
        assert config.blob_cache_enabled is None

    def test_retention_max_recent_matches_retention_policy_default(self):
        """retention_max_recent must equal RetentionPolicy.max_recent_versions so GC and config stay aligned."""
        from vfs.config import VFSConfig
        from vfs.models import RetentionPolicy

        assert VFSConfig().retention_max_recent == RetentionPolicy().max_recent_versions


class TestVFSConfigEnvOverride:
    """Environment variable overrides."""

    def test_env_prefix_overrides(self, monkeypatch):
        from vfs.config import VFSConfig

        monkeypatch.setenv("AIFS_METADATA_STORE_URI", "sqlite:///custom.db")
        config = VFSConfig()
        assert config.metadata_store_uri == "sqlite:///custom.db"

    def test_env_blob_store_override(self, monkeypatch):
        from vfs.config import VFSConfig

        monkeypatch.setenv("AIFS_BLOB_STORE_URI", "s3://my-bucket/")
        config = VFSConfig()
        assert config.blob_store_uri == "s3://my-bucket/"
