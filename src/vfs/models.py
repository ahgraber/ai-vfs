"""VFS domain models."""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum

from pydantic import BaseModel, Field


class SearchType(Enum):
    """Supported search strategies."""

    GLOB = "glob"
    FIND = "find"
    REGEX = "regex"
    FULLTEXT = "fulltext"
    SEMANTIC = "semantic"


class RetentionTier(BaseModel):
    """A single tier in a retention policy."""

    max_age: timedelta
    keep_every: timedelta | None


def _default_tiers() -> list[RetentionTier]:
    """Default Time Machine-style retention tiers per spec DefaultRetention."""
    return [
        RetentionTier(max_age=timedelta(hours=24), keep_every=None),
        RetentionTier(max_age=timedelta(days=7), keep_every=timedelta(hours=1)),
        RetentionTier(max_age=timedelta(days=30), keep_every=timedelta(days=1)),
        RetentionTier(max_age=timedelta.max, keep_every=timedelta(weeks=1)),
    ]


class RetentionPolicy(BaseModel):
    """Version retention configuration."""

    max_recent_versions: int = 50
    tiers: list[RetentionTier] = Field(default_factory=_default_tiers)
    keep_first_version: bool = True
    keep_current_version: bool = True


class FileMeta(BaseModel):
    """Metadata for a file entry."""

    namespace_id: str
    path: str
    current_version_id: str
    current_version_number: int
    created_at: datetime
    updated_at: datetime
    is_deleted: bool = False


class VersionMeta(BaseModel):
    """Metadata for a single version of a file."""

    id: str
    file_path: str
    namespace_id: str
    version_number: int
    content_hash: str
    size: int
    created_at: datetime
    created_by: str
    is_tombstone: bool = False
    search_meta: dict = {}
    parent_version_id: str | None = None


class Permission(BaseModel):
    """An access-control entry."""

    id: str
    principal_id: str
    namespace_id: str
    path_prefix: str
    operations: set[str]
    created_at: datetime


class AuditEvent(BaseModel):
    """An append-only audit log entry."""

    event_id: str
    timestamp: datetime
    namespace_id: str
    principal_id: str
    operation: str
    path: str | None = None
    version_id: str | None = None
    detail: dict = {}
    trace_id: str | None = None


class SearchResult(BaseModel):
    """A single search hit."""

    path: str
    line_number: int | None = None
    match_context: str | None = None
    score: float = 1.0


class Namespace(BaseModel):
    """A namespace (workspace) entry."""

    id: str
    display_name: str
    created_at: datetime
    created_by: str
    retention_policy: RetentionPolicy | None = None


class Principal(BaseModel):
    """An identity (user, agent, or service)."""

    id: str
    display_name: str
    principal_type: str
    created_at: datetime
