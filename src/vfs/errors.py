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


class AnchorConflictError(VFSError):
    """An anchored edit could not be applied safely and was rejected without writing.

    Anchors are stateless ``{absolute_line_index}:{checksum}`` tokens derived
    purely from file content (see :mod:`vfs.anchored_editing`). This error is
    raised when:

    - The file's current version differs from the edit's ``expected_version``
      (the file changed since the anchors were read).
    - An anchor's checksum does not match the content at its index (a fabricated
      anchor, an index transposition, or an anchor pasted from another file).
    - An anchor's index is out of range, or a hunk's end anchor resolves before
      its start anchor (inverted range), or hunks overlap.
    - An anchor token is malformed (not ``index:checksum``).
    - ``session.write`` raises ``ConflictError`` or ``VersionCollisionError``
      during the CAS write (always surfaced as ``AnchorConflictError`` — never
      retried).

    The caller should re-read the file (``read_anchored``) to obtain fresh
    anchors and the current version before retrying the edit.
    """


class ContentDecodeError(VFSError):
    """File content could not be decoded as strict UTF-8 for an anchored read/edit.

    Anchored editing operates on text lines; binary or non-UTF-8 content cannot
    be anchored. The operation yields no anchors and writes nothing.
    """


class UnsupportedOperationError(VFSError):
    """A filesystem operation with no VFS equivalent was requested.

    The VFS has no symbolic links, permission modes, or modification-time
    metadata, so the FS-port raises this rather than silently succeeding when a
    sandbox requests ``symlink``/``readlink``/``chmod``/``utime`` and similar.
    """
