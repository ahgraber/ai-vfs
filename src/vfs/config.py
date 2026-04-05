"""VFS configuration via pydantic-settings."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class VFSConfig(BaseSettings):
    """Configuration for the VFS library."""

    model_config = SettingsConfigDict(env_prefix="AIFS_")

    metadata_store_uri: str = "sqlite:///./aifs.db"
    blob_store_uri: str = "file:///./aifs_blobs/"
    blob_cache_enabled: bool | None = None
    blob_cache_max_size_mb: int = 1024
    blob_cache_dir: str | None = None
    retention_max_recent: int = 50
    retention_tiers: list[dict] | None = None
    otel_enabled: bool = True
    audit_log_enabled: bool = True
    search_providers: list[str] = ["default"]
    execution_providers: list[str] = []
    default_timeout_seconds: float = 30.0
    default_max_operations: int = 1000
