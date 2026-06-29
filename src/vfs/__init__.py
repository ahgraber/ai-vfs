"""AI-first virtual file system."""

from vfs.anchored_editing import (
    AnchoredEditor,
    AnchoredEditResult,
    AnchoredReadResult,
    Hunk,
    make_anchor,
)
from vfs.config import VFSConfig
from vfs.errors import (
    AnchorConflictError,
    ConflictError,
    ContentDecodeError,
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
    # Anchored editing (standalone capability)
    "AnchoredEditor",
    "AnchoredEditResult",
    "AnchoredReadResult",
    "Hunk",
    "make_anchor",
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
    "AnchorConflictError",
    "ContentDecodeError",
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
