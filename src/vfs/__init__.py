"""AI-first virtual file system."""

from vfs.config import VFSConfig
from vfs.errors import ConflictError, NotFoundError, PermissionDeniedError, VFSError
from vfs.execution.fs_ops import FsOperations, fs_operations_for
from vfs.models import GCResult
from vfs.protocols.execution import ExecutionCapabilities, ExecutionProvider, ExecutionResult, ResourceLimits
from vfs.session import Session, resolve_path
from vfs.vfs import VFS

__all__ = [
    "VFS",
    "VFSConfig",
    "ExecutionCapabilities",
    "ExecutionProvider",
    "ExecutionResult",
    "FsOperations",
    "GCResult",
    "ConflictError",
    "NotFoundError",
    "PermissionDeniedError",
    "ResourceLimits",
    "Session",
    "VFSError",
    "fs_operations_for",
    "resolve_path",
]
