"""VFS error hierarchy."""


class VFSError(Exception):
    """Base exception for all VFS errors."""


class ConflictError(VFSError):
    """CAS version mismatch."""


class PermissionDeniedError(VFSError):
    """Principal lacks required permission."""


class NotFoundError(VFSError):
    """Requested resource does not exist."""
