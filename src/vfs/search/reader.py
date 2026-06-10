"""Guarded ContentReader for search straggler verification."""

from __future__ import annotations

from typing import Any

from vfs.errors import PermissionDeniedError, ReadBudgetExceededError
from vfs.protocols.search import SearchMetaEntry


class ContentReader:
    r"""Content reader bound to a permission-pruned set of enumerated file versions.

    The VFS constructs one per search request.  Its contract:

    - Resolves ``path`` to the **enumerated version's** ``content_hash`` (never
      latest-by-path), so concurrent writes do not affect in-flight verification.
    - Enforces ``max_content_reads`` as a hard ceiling; the ``(N+1)``\th call
      raises :class:`~vfs.errors.ReadBudgetExceededError`.
    - Refuses paths not present in the enumerated ``entries``; raises
      :class:`~vfs.errors.PermissionDeniedError`.

    The reader is used **only** for bounded straggler verification — fresh
    native-FTS matches never touch it.
    """

    def __init__(
        self,
        entries: list[SearchMetaEntry],
        blob: Any,  # BlobStore — typed Any to avoid a circular protocol import
        max_reads: int,
    ) -> None:
        self._index: dict[str, SearchMetaEntry] = {e.path: e for e in entries}
        self._blob = blob
        self._max_reads = max_reads
        self._reads_done: int = 0

    @property
    def reads_done(self) -> int:
        """Number of successful blob reads performed so far."""
        return self._reads_done

    @property
    def reads_remaining(self) -> int:
        """Remaining reads before the budget ceiling is hit."""
        return self._max_reads - self._reads_done

    async def read(self, path: str) -> bytes:
        """Return the content of the enumerated version at ``path``.

        The blob is fetched by the entry's ``content_hash`` (immutable), not by
        path, so the result is immune to concurrent writes.

        Raises
        ------
            PermissionDeniedError: ``path`` is not in the enumerated scope.
            ReadBudgetExceededError: the ``max_content_reads`` ceiling has been reached.
        """
        if path not in self._index:
            raise PermissionDeniedError(f"Path {path!r} is outside the search scope; read refused")
        if self._reads_done >= self._max_reads:
            raise ReadBudgetExceededError(f"Content read budget ({self._max_reads}) exhausted")
        entry = self._index[path]
        data = await self._blob.get(entry.content_hash)
        self._reads_done += 1
        return data
