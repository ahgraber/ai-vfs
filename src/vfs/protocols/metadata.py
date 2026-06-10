"""MetadataStore protocol definition."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, AsyncIterator, Protocol, runtime_checkable

from vfs.models import (
    AuditEvent,
    FileMeta,
    Namespace,
    Permission,
    Principal,
    RetentionPolicy,
    SearchArtifact,
    VersionMeta,
)

if TYPE_CHECKING:
    from vfs.protocols.search import NativeTextSearch


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

    async def has_any_admin(self, namespace_id: str) -> bool:
        """Return True if any principal holds an `admin` operation grant in the namespace."""
        ...

    # --- Audit ---

    async def append_audit_event(self, event: AuditEvent) -> None:
        """Append an immutable audit event record."""
        ...

    # --- Search metadata ---

    async def update_search_meta(self, version_id: str, search_meta: dict) -> None:
        """Attach provider-returned search metadata to a version record."""
        ...

    async def get_search_meta_batch(self, version_ids: list[str]) -> dict[str, dict]:
        """Return the ``search_meta`` manifest for each of the given version IDs.

        Keys in the returned dict are version IDs; values are the raw
        ``search_meta`` dicts (provider key → serialized artifact dict).
        Version IDs with no matching record are omitted from the result.
        """
        ...

    async def update_search_artifact(self, version_id: str, provider_key: str, artifact: SearchArtifact) -> None:
        """Set a single provider artifact in the ``search_meta`` manifest.

        Merges ``{provider_key: artifact.to_dict()}`` into the existing manifest
        for ``version_id``, preserving any other provider keys already present.
        A no-op if ``version_id`` does not exist.
        """
        ...

    def native_text_search(self) -> NativeTextSearch | None:
        """Return the :class:`~vfs.protocols.search.NativeTextSearch` capability, or ``None``.

        Stores that implement native text indexing (SQLite FTS5, PostgreSQL ``tsvector`` +
        ``pg_trgm``) return the capability object; stores without it (MongoDB, and SQL
        stores before the capability is activated) return ``None``.

        Returning ``None`` means regex search falls back to the ``DefaultSearchProvider``
        brute-force path (which uses the guarded reader budget and fails loud on
        over-budget scope) and fulltext search is rejected with
        :class:`~vfs.errors.SearchTypeUnsupportedError`.
        """
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

    def iter_versions_for_gc(self, namespace_id: str, file_path: str) -> AsyncIterator[VersionMeta]:
        """Yield all non-tombstone versions for a file in deterministic order (created_at, version_number)."""
        ...

    async def delete_versions(self, version_ids: list[str]) -> None:
        """Permanently remove the given version records."""
        ...

    async def has_version_references(self, content_hash: str) -> bool:
        """Return True if any version record references the given content hash."""
        ...

    # --- Entity persistence ---

    async def put_namespace(self, namespace: Namespace) -> None:
        """Persist a namespace record."""
        ...

    async def put_principal(self, principal: Principal) -> None:
        """Persist a principal record."""
        ...

    # --- Transactions ---

    def transaction(self) -> AsyncIterator[None]:
        """Return an async context manager that wraps operations in a transaction."""
        ...
