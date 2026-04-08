"""AI-first virtual file system."""

from vfs.config import VFSConfig
from vfs.errors import ConflictError, NotFoundError, PermissionDeniedError, VFSError
from vfs.models import GCResult
from vfs.vfs import VFS

__all__ = [
    "VFS",
    "VFSConfig",
    "GCResult",
    "ConflictError",
    "NotFoundError",
    "PermissionDeniedError",
    "VFSError",
]
