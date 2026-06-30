"""AI-first virtual file system."""

from vfs.config import VFSConfig
from vfs.errors import (
    ConflictError,
    IndexUnavailableError,
    NotFoundError,
    OperationBudgetExceededError,
    PermissionDeniedError,
    ReadBudgetExceededError,
    ReindexRequiredError,
    SearchTypeUnsupportedError,
    VersionCollisionError,
    VFSError,
)
from vfs.execution.fs_ops import FsOperations, fs_operations_for
from vfs.execution.registry import resolve_execution_provider
from vfs.models import GCResult
from vfs.protocols.execution import ExecutionCapabilities, ExecutionProvider, ExecutionResult, ResourceLimits
from vfs.session import Session, resolve_path
from vfs.vfs import VFS

__all__ = [
    "VFS",
    "VFSConfig",
    # Execution
    "ExecutionCapabilities",
    "ExecutionProvider",
    "ExecutionResult",
    "FsOperations",
    "ResourceLimits",
    "fs_operations_for",
    "resolve_execution_provider",
    # Models / session
    "GCResult",
    "Session",
    "resolve_path",
    # Error types callers must catch
    "VFSError",
    "ConflictError",
    "IndexUnavailableError",
    "NotFoundError",
    "OperationBudgetExceededError",
    "PermissionDeniedError",
    "ReadBudgetExceededError",
    "ReindexRequiredError",
    "SearchTypeUnsupportedError",
    "VersionCollisionError",
]
