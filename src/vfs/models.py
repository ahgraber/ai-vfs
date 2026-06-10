"""VFS domain models."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

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


class GCResult(BaseModel):
    """Result of a garbage collection run."""

    versions_reclaimed: int
    blobs_reclaimed: int


@dataclass(frozen=True)
class SearchArtifact:
    """Envelope representing one provider's search index artifact for a file version.

    ``status`` is one of ``"ready"``, ``"failed"``, or ``"unsupported"``.
    ``storage`` is one of ``"inline"``, ``"blob"``, or ``"external"``.

    Usability check:
    - ``status == "ready"`` AND ``content_hash`` matches the version's content hash AND
      ``params_hash`` matches the active provider's config hash.
    - For ``storage == "external"``: additionally the referenced record must be readable
      and its recorded identity must match (``content_hash``/``params_hash``); a missing,
      unreadable, or mismatched record is treated as a straggler, never a confirmed
      non-match.

    Serialization: use :meth:`to_dict` / :meth:`from_dict` at the store boundary.
    The ``created_at`` datetime is stored as an ISO-8601 string in the serialized form.
    """

    status: str
    schema_version: int
    provider_key: str
    provider_version: str
    params_hash: str
    content_hash: str
    created_at: datetime
    storage: str
    payload: Any = None
    artifact_ref: str | None = None
    error_code: str | None = None
    error_message: str | None = None

    def is_usable(
        self,
        *,
        current_content_hash: str,
        active_params_hash: str,
        external_readable: bool = True,
        external_identity_match: bool = True,
    ) -> bool:
        """Return True iff the artifact can answer a search without re-indexing.

        For ``storage == "external"``, the caller must also supply the external record's
        readability and identity-match status (``external_readable``,
        ``external_identity_match``); a missing or mismatched record is a straggler.
        """
        if self.status != "ready":
            return False
        if self.content_hash != current_content_hash:
            return False
        if self.params_hash != active_params_hash:
            return False
        if self.storage == "external":
            return external_readable and external_identity_match
        return True

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict for storage in the ``search_meta`` manifest.

        ``created_at`` is stored as an ISO-8601 string; all other fields are
        JSON-primitive or ``None``.
        """
        d = dataclasses.asdict(self)
        d["created_at"] = self.created_at.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SearchArtifact:
        """Reconstruct a :class:`SearchArtifact` from a serialized dict.

        Accepts either an ISO-8601 string or an already-parsed ``datetime`` for
        ``created_at`` so the method is idempotent across store round-trips.
        """
        d = dict(d)
        if isinstance(d.get("created_at"), str):
            d["created_at"] = datetime.fromisoformat(d["created_at"])
        return cls(**d)
