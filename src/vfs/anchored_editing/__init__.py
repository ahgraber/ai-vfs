"""Anchored editing: stateless, content-derived, line-precise edits over the VFS.

This capability is independent of any execution sandbox. An agent framework can
call :class:`AnchoredEditor` directly as a tool, and the Monty sandbox's ``edit``
verb delegates to the same surface.
"""

from vfs.anchored_editing.anchors import (
    K_DEFAULT,
    anchors_for_lines,
    checksum,
    make_anchor,
    parse_anchor,
    resolve_anchor,
)
from vfs.anchored_editing.editor import (
    AnchoredEditor,
    AnchoredEditResult,
    AnchoredReadResult,
    Hunk,
)

__all__ = [
    "K_DEFAULT",
    "AnchoredEditResult",
    "AnchoredEditor",
    "AnchoredReadResult",
    "Hunk",
    "anchors_for_lines",
    "checksum",
    "make_anchor",
    "parse_anchor",
    "resolve_anchor",
]
