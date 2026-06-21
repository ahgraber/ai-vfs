"""Garbage collector for version and blob cleanup."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Sequence

from vfs.config import VFSConfig
from vfs.models import GCResult, RetentionPolicy, RetentionTier, VersionMeta
from vfs.observability.audit import audit_gc_run


def evaluate_tier_retention(
    versions: Sequence[VersionMeta],
    policy: RetentionPolicy,
    now: datetime,
    current_version_id: str | None = None,
) -> set[str]:
    """Return the set of version IDs that are reclaimable under the tier rules of *policy*.

    The *versions* sequence may arrive in any order; the function is deterministic
    regardless of enumeration order.  ``now`` is the reference time for age
    calculations and must be injected by the caller — no ``datetime.now()`` call
    is made here, keeping the function a pure unit-testable computation.

    Always-keep rules:

    * ``keep_first_version`` — the version with the smallest (created_at, version_number)
      is never returned as reclaimable.
    * ``keep_current_version`` — the version identified by *current_version_id* is never
      returned as reclaimable.

    Tier rules (applied only when ``policy.tiers`` is non-empty):

    * Tiers are evaluated newest-first (ascending ``max_age``).  The band for tier *i* is
      ``[prev_max_age, tier.max_age)``, where *prev_max_age* is 0 for tier 0 and the
      previous tier's ``max_age`` for tier *i > 0*.
    * Within a band with ``keep_every=None``: all versions are kept.
    * Within a band with ``keep_every`` set: versions are grouped into consecutive windows
      of width ``keep_every`` anchored at *now* (``window_index = floor(age / keep_every)``).
      For each window the version with the smallest *created_at* (then *version_number* as a
      tie-breaker) is the survivor; the rest are reclaimable.
    * Versions whose age >= the last tier's ``max_age`` are outside all tiers and reclaimable
      (subject to the always-keep rules above).

    When ``policy.tiers`` is empty, only the always-keep rules apply; every other version
    is reclaimable.
    """
    if not versions:
        return set()

    keep: set[str] = set()

    # Always-keep: first version (smallest created_at, then version_number as tie-breaker)
    if policy.keep_first_version:
        first = min(versions, key=lambda v: (v.created_at, v.version_number))
        keep.add(first.id)

    # Always-keep: current version
    if policy.keep_current_version and current_version_id is not None:
        keep.add(current_version_id)

    if not policy.tiers:
        return {v.id for v in versions} - keep

    # Tier-band pass: determine which versions are kept by tier rules.
    prev_max_age: timedelta = timedelta(0)
    for tier in policy.tiers:
        # Versions whose age falls in [prev_max_age, tier.max_age).
        band = [v for v in versions if prev_max_age <= (now - v.created_at) < tier.max_age]

        if tier.keep_every is None:
            # Keep all versions in this band.
            keep.update(v.id for v in band)
        else:
            # Keep the survivor (smallest created_at) of each keep_every window.
            keep_every_secs = tier.keep_every.total_seconds()
            windows: dict[int, list[VersionMeta]] = {}
            for v in band:
                age_secs = (now - v.created_at).total_seconds()
                window_idx = int(age_secs / keep_every_secs)
                windows.setdefault(window_idx, []).append(v)
            for window_versions in windows.values():
                survivor = min(window_versions, key=lambda v: (v.created_at, v.version_number))
                keep.add(survivor.id)

        prev_max_age = tier.max_age

    # Versions outside all tiers (age >= last tier's max_age) are reclaimable — already
    # absent from keep; always-keep rules have been applied above.
    return {v.id for v in versions} - keep


class GarbageCollector:
    """Two-phase garbage collector: version GC then blob GC."""

    def __init__(self, meta_store, blob_store, config: VFSConfig) -> None:
        self._meta = meta_store
        self._blob = blob_store
        self._config = config

    async def run(self, namespace_id: str | None = None) -> GCResult:
        """Run version GC then blob GC; return counts of reclaimed items.

        The version GC path is selected based on ``VFSConfig.retention_tiers``:

        * **Simple path** (default, ``retention_tiers`` is ``None``): calls
          ``_version_gc``, which applies ``max_recent_versions`` via
          ``list_reclaimable_versions``. Backward-compatible; existing deployments
          that rely only on ``retention_max_recent`` are unaffected.
        * **Tier path** (``retention_tiers`` is a non-empty list of tier dicts):
          calls ``_tier_version_gc`` with a ``RetentionPolicy`` built from both
          ``retention_tiers`` and ``retention_max_recent``.  Tier GC never reclaims
          tombstone versions; for deleted files the last content version is treated
          as current (over-retention, never loss).

        Per-namespace ``Namespace.retention_policy`` overrides are not yet wired
        (no ``get_namespace`` on the ``MetadataStore`` protocol); that is a future
        extension. The config-level policy applies uniformly to all namespaces.
        """
        try:
            import setproctitle

            setproctitle.setproctitle("ai-vfs: gc")
        except ModuleNotFoundError:
            pass

        if self._config.retention_tiers:
            policy = RetentionPolicy(
                max_recent_versions=self._config.retention_max_recent,
                tiers=[RetentionTier.model_validate(d) for d in self._config.retention_tiers],
            )
            versions_reclaimed = await self._tier_version_gc(namespace_id, policy)
        else:
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

    async def _tier_version_gc(
        self,
        namespace_id: str | None,
        policy: RetentionPolicy,
        now: datetime | None = None,
    ) -> int:
        """Run tier-based version GC and return the count of versions deleted.

        Enumerates files via :meth:`~vfs.protocols.metadata.MetadataStore.list_reclaimable_versions`
        (used as a coarse file-enumerator with a permissive policy), then fetches each
        file's versions in deterministic order via
        :meth:`~vfs.protocols.metadata.MetadataStore.iter_versions_for_gc` and evaluates
        tier reclamation with the injected *now* reference time.

        Keeping this separate from :meth:`_version_gc` preserves the unchanged Phase 1 simple
        path (``list_reclaimable_versions``) while adding tier-aware reclamation on top.
        """
        if now is None:
            now = datetime.now(timezone.utc)

        # Use list_reclaimable_versions with a permissive policy to enumerate
        # all files that have at least one non-tombstone version.
        enum_policy = RetentionPolicy(
            max_recent_versions=0,
            tiers=[],
            keep_first_version=False,
            keep_current_version=False,
        )
        all_versions = await self._meta.list_reclaimable_versions(enum_policy, namespace_id)

        # Collect unique (namespace_id, file_path) keys.
        file_keys: set[tuple[str, str]] = {(v.namespace_id, v.file_path) for v in all_versions}

        reclaimable_ids: list[str] = []
        for ns_id, file_path in file_keys:
            # iter_versions_for_gc guarantees deterministic (created_at, version_number) order.
            versions: list[VersionMeta] = []
            async for v in self._meta.iter_versions_for_gc(ns_id, file_path):
                versions.append(v)
            if not versions:
                continue
            # Treat the highest version_number as the current version for keep_current_version.
            current_version_id = max(versions, key=lambda v: v.version_number).id
            reclaimable_ids.extend(evaluate_tier_retention(versions, policy, now, current_version_id))

        if reclaimable_ids:
            await self._meta.delete_versions(reclaimable_ids)
        return len(reclaimable_ids)

    async def _blob_gc(self) -> int:
        """Sweep orphaned blobs; also delete their text artifacts to keep metadata consistent.

        The reference check and the text-artifact deletion run inside one metadata transaction,
        which holds the store lock for the critical section.  A concurrent write that revives a
        ``content_hash`` therefore cannot interleave between the check and the deletion: it either
        commits before the check (the live reference is seen and the hash is skipped) or after the
        transaction (it re-indexes the text artifact via the write path).  This keeps a
        live-referenced ``content_hash`` from ever having its text artifacts swept — the invariant
        the removed query-time existence re-check incidentally guarded.

        The blob delete follows the committed transaction; the cross-store blob/metadata race is
        inherent (two independent stores, no shared transaction) and unchanged by this change.
        Operations are idempotent, so a retry safely re-visits any partially-cleaned hash.
        """
        nts = self._meta.native_text_search()
        count = 0
        async for content_hash in self._blob.list_hashes():
            async with self._meta.transaction():
                if await self._meta.has_version_references(content_hash):
                    continue
                if nts is not None:
                    await nts.delete_text_artifacts([content_hash], [])
            await self._blob.delete(content_hash)
            count += 1
        return count

    async def sweep_retired_text_params(self, retired_params_hashes: list[str]) -> None:
        """Delete text artifacts whose params_hash profile has been retired.

        Call this when the NativeTextSearch tokenizer/extractor configuration changes:
        the old params_hash records are no longer needed and can be reclaimed.  A
        subsequent write or reindex will produce new records under the current params_hash.
        """
        nts = self._meta.native_text_search()
        if nts is not None:
            await nts.delete_text_artifacts([], retired_params_hashes)
