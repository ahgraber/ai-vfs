"""VFS error hierarchy."""


class VFSError(Exception):
    """Base exception for all VFS errors."""


class ConflictError(VFSError):
    """CAS version mismatch."""


class VersionCollisionError(VFSError):
    """Concurrent no-CAS writes collided on the same version number; the caller should retry."""


class PermissionDeniedError(VFSError):
    """Principal lacks required permission."""


class NotFoundError(VFSError):
    """Requested resource does not exist."""


class ReadBudgetExceededError(VFSError):
    """Content-read budget for a search operation was exhausted."""


class SearchTypeUnsupportedError(VFSError):
    """The requested search type is not supported by the active metadata backend."""


class IndexUnavailableError(VFSError):
    """The native search index is unavailable and the search cannot be served.

    Raised when the index store raises an exception during a search call.
    Run ``vfs.reindex(namespace_id)`` to rebuild the index after the
    underlying store issue is resolved.
    """


class ReindexRequiredError(VFSError):
    """Too many files lack a fresh search index; run ``vfs.reindex`` before searching.

    Raised when the straggler count (files without a usable search artifact)
    exceeds the ``SearchLimits.max_content_reads`` budget.  Serving the search
    would require unbounded blob reads, which is never performed silently.
    Run ``vfs.reindex(namespace_id)`` to rebuild the index.
    """


class OperationBudgetExceededError(VFSError):
    """The shell-operations budget for a single execution was exhausted.

    Raised by the ``OperationCounter`` wrapper inside ``fs_operations_for`` when
    the number of VFS callback invocations reaches ``ResourceLimits.max_operations``.
    The underlying VFS operation is NOT invoked when this error is raised.
    """


class UnsupportedOperationError(VFSError):
    """A filesystem operation with no VFS equivalent was requested.

    The VFS has no symbolic links, permission modes, or modification-time
    metadata, so the FS-port raises this rather than silently succeeding when a
    sandbox requests ``symlink``/``readlink``/``chmod``/``utime`` and similar.
    """
