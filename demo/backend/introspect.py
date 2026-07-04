"""Read-only VFS introspection — a window into the live store the agent mutates.

`tree`/`cat`/`view_diff` become read-only functions the app exposes as JSON, so an
inspector panel can render the live tree, a file's current version, and diffs
between versions while you chat. All reads go through a full-visibility principal;
none of these mutate anything.
"""

from __future__ import annotations

import difflib

from vfs import VFS, Session


async def tree(vfs: VFS, ns_id: str, principal_id: str, prefix: str = "/") -> list[str]:
    """Every path under `prefix`, sorted."""
    session = Session(vfs, ns_id, principal_id)
    metas = await session.list(prefix, recursive=True)
    return sorted(m.path for m in metas)


async def read_file(vfs: VFS, ns_id: str, principal_id: str, path: str, version_number: int | None = None) -> dict:
    """Return a file's content at `version_number` (default: current) plus its version list."""
    session = Session(vfs, ns_id, principal_id)
    history = await session.versions(path)  # newest-first
    body = (await session.read(path, version_number=version_number)).decode("utf-8", errors="replace")
    current = version_number if version_number is not None else (history[0].version_number if history else None)
    return {
        "path": path,
        "version_number": current,
        "content": body,
        "versions": [{"version_number": v.version_number, "size": v.size} for v in history],
    }


async def diff(
    vfs: VFS,
    ns_id: str,
    principal_id: str,
    path: str,
    older: int | None = None,
    newer: int | None = None,
) -> dict:
    """Unified diff between two versions of `path` (defaults to the two newest)."""
    session = Session(vfs, ns_id, principal_id)
    history = await session.versions(path)  # newest-first
    if len(history) < 2:
        only = history[0].version_number if history else None
        return {"path": path, "older": only, "newer": only, "diff": "", "note": "only one version; nothing to diff"}
    newer = newer if newer is not None else history[0].version_number
    older = older if older is not None else history[1].version_number
    old_text = (await session.read(path, version_number=older)).decode("utf-8", errors="replace")
    new_text = (await session.read(path, version_number=newer)).decode("utf-8", errors="replace")
    lines = difflib.unified_diff(
        old_text.splitlines(),
        new_text.splitlines(),
        fromfile=f"{path}@v{older}",
        tofile=f"{path}@v{newer}",
        lineterm="",
    )
    return {"path": path, "older": older, "newer": newer, "diff": "\n".join(lines)}
