"""Garbage collector for version and blob cleanup."""

from __future__ import annotations

from vfs.config import VFSConfig
from vfs.models import GCResult, RetentionPolicy
from vfs.observability.audit import audit_gc_run


class GarbageCollector:
    """Two-phase garbage collector: version GC then blob GC."""

    def __init__(self, meta_store, blob_store, config: VFSConfig) -> None:
        self._meta = meta_store
        self._blob = blob_store
        self._config = config

    async def run(self, namespace_id: str | None = None) -> GCResult:
        """Run version GC then blob GC; return counts of reclaimed items."""
        try:
            import setproctitle

            setproctitle.setproctitle("ai-vfs: gc")
        except ModuleNotFoundError:
            pass
        versions_reclaimed = await self._version_gc(namespace_id)
        blobs_reclaimed = await self._blob_gc()
        await audit_gc_run(
            self._meta,
            namespace_id=namespace_id,
            versions_reclaimed=versions_reclaimed,
            blobs_reclaimed=blobs_reclaimed,
            audit_log_enabled=self._config.audit_log_enabled,
        )
        return GCResult(
            versions_reclaimed=versions_reclaimed,
            blobs_reclaimed=blobs_reclaimed,
        )

    async def _version_gc(self, namespace_id: str | None) -> int:
        policy = RetentionPolicy(
            max_recent_versions=self._config.retention_max_recent,
        )
        reclaimable = await self._meta.list_reclaimable_versions(policy, namespace_id)
        ids = [v.id for v in reclaimable]
        if ids:
            await self._meta.delete_versions(ids)
        return len(ids)

    async def _blob_gc(self) -> int:
        count = 0
        async for content_hash in self._blob.list_hashes():
            if not await self._meta.has_version_references(content_hash):
                await self._blob.delete(content_hash)
                count += 1
        return count
