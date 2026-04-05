"""MetadataStore protocol definition."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator, Protocol, runtime_checkable

from vfs.models import (
    AuditEvent,
    FileMeta,
    Namespace,
    Permission,
    Principal,
    RetentionPolicy,
    VersionMeta,
)


@runtime_checkable
class MetadataStore(Protocol):
    """Metadata storage for files, versions, permissions, audit, and names."""

    async def initialize(self) -> None:
        """Set up storage backend (create tables, indices, etc.)."""
        ...

    # --- File operations ---

    async def put_file(self, file_meta: FileMeta) -> None:
        """Insert or replace a file record."""
        ...

    async def get_file(self, namespace_id: str, path: str) -> FileMeta | None:
        """Return the file record for the given namespace and path, or None."""
        ...

    async def delete_file(self, namespace_id: str, path: str) -> None:
        """Remove the file record for the given namespace and path."""
        ...

    async def list_dir(self, namespace_id: str, path_prefix: str, *, recursive: bool = False) -> list[FileMeta]:
        """List files under path_prefix; recurse into subdirectories when recursive=True."""
        ...

    # --- Version operations ---

    async def put_version(self, version: VersionMeta, *, expected_version: int | None = None) -> None:
        """Store a new version; raise on optimistic-concurrency conflict when expected_version is set."""
        ...

    async def get_version(self, namespace_id: str, path: str, version_number: int | None = None) -> VersionMeta | None:
        """Return a specific version (or the latest when version_number is None), or None."""
        ...

    async def list_versions(
        self,
        namespace_id: str,
        path: str,
        *,
        limit: int = 50,
        before: int | None = None,
    ) -> list[VersionMeta]:
        """Return up to limit versions older than before (exclusive), newest-first."""
        ...

    # --- Permissions ---

    async def check_permission(self, principal_id: str, namespace_id: str, path: str, operation: str) -> bool:
        """Return True if the principal is allowed to perform operation on path."""
        ...

    async def set_permission(self, permission: Permission) -> None:
        """Insert or replace a permission record."""
        ...

    # --- Audit ---

    async def append_audit_event(self, event: AuditEvent) -> None:
        """Append an immutable audit event record."""
        ...

    # --- Search metadata ---

    async def update_search_meta(self, version_id: str, search_meta: dict) -> None:
        """Attach provider-returned search metadata to a version record."""
        ...

    # --- Name resolution ---

    async def set_name(self, entity_type: str, entity_id: str, display_name: str) -> None:
        """Register or update a display name for an entity."""
        ...

    async def resolve_name(self, entity_type: str, display_name: str) -> str | None:
        """Return the entity ID for a display name, or None if not found."""
        ...

    # --- GC ---

    async def list_reclaimable_versions(
        self, policy: RetentionPolicy, namespace_id: str | None = None
    ) -> list[VersionMeta]:
        """Return versions eligible for garbage collection under the given policy."""
        ...

    async def delete_versions(self, version_ids: list[str]) -> None:
        """Permanently remove the given version records."""
        ...

    # --- Transactions ---

    def transaction(self) -> AsyncIterator[None]:
        """Return an async context manager that wraps operations in a transaction."""
        ...
