"""FsOperations dataclass and fs_operations_for factory.

``FsOperations`` is the session-bound bridge between a sandboxed execution
provider and the VFS.  Every callable in the dataclass is wrapped by a shared
``OperationCounter`` that raises ``OperationBudgetExceededError`` once the
``max_operations`` budget is exhausted.

Design decisions
----------------
- ``grep``, ``find``, and ``glob`` call ``session.search(...)`` directly now
  that ``Session.search`` accepts ``find_predicates``.
- ``ls`` size batching: ``size`` lives on ``VersionMeta``, not ``FileMeta``.
  For ``ls(long=True)`` we fetch ``VersionMeta`` per entry via
  ``session._vfs._meta.get_version`` (one call per entry, sequentially).
  ``get_search_meta_batch`` is a search-artifact utility and cannot serve this
  purpose.  Batching would require a new ``MetadataStore`` method (out of scope
  here); the current implementation is correct and documented.
  TODO(perf): add a batch ``get_versions`` method to MetadataStore if ``ls
  --long`` proves to be a hot path.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import posixpath
from typing import TYPE_CHECKING, Any

from vfs.errors import AnchorConflictError, ConflictError, OperationBudgetExceededError, VersionCollisionError
from vfs.models import SearchType
from vfs.protocols.search import FindPredicates
from vfs.session import resolve_path

if TYPE_CHECKING:
    from vfs.protocols.execution import ResourceLimits
    from vfs.session import Session

_log = logging.getLogger(__name__)


class OperationCounter:
    """Shared counter that raises ``OperationBudgetExceededError`` at the limit.

    The counter is incremented BEFORE the underlying operation is invoked;
    when the limit is reached the operation is blocked and the error is raised.
    """

    def __init__(self, max_operations: int) -> None:
        self._max = max_operations
        self._count = 0

    def check_and_increment(self) -> None:
        """Increment counter; raise ``OperationBudgetExceededError`` if limit reached."""
        if self._count >= self._max:
            raise OperationBudgetExceededError(f"Operation budget exhausted ({self._max} operations).")
        self._count += 1

    @property
    def count(self) -> int:
        """Current operation count."""
        return self._count


@dataclass
class FsOperations:
    """Session-bound shell-operation callables for sandboxed execution.

    All public fields are async callables corresponding to the eleven shell
    wrappers (``cd``, ``pwd``, ``cat``, ``head``, ``tail``, ``ls``, ``grep``,
    ``find``, ``glob``, ``write``, ``edit``) plus internal fields (``read``,
    ``stat``, ``delete``) for use within the execution layer.

    Every callable (except ``pwd`` and ``cd``, which never touch data) resolves
    relative paths through the session's ``cwd`` before invoking the underlying
    VFS operation.

    Use :func:`fs_operations_for` to construct an instance; do not instantiate
    directly.
    """

    # Public shell wrappers
    cd: Any
    pwd: Any
    cat: Any
    head: Any
    tail: Any
    ls: Any
    grep: Any
    find: Any
    glob: Any
    write: Any
    edit: Any

    # Internal primitives (not part of the shell surface; used by edit and tests)
    read: Any
    stat: Any
    delete: Any


# ---------------------------------------------------------------------------
# Module-level helpers (used both directly and by closure wrappers below)
# ---------------------------------------------------------------------------


def _error_response(code: str, message: str) -> dict:
    """Return a structured read-error dict with empty lines and anchors."""
    return {"lines": [], "anchors": {}, "error": {"code": code, "message": message}}


def _decode_raw(raw: bytes, max_read_bytes: int | None, path: str) -> str | dict:
    """Decode ``raw`` bytes as strict UTF-8, enforcing the read-bytes cap.

    Returns the decoded ``str`` on success, or an error-response dict on failure.
    Callers check ``isinstance(result, dict)`` and return it directly.
    """
    if max_read_bytes is not None and len(raw) > max_read_bytes:
        _log.debug("read: oversized (%d bytes > %d limit) for %s", len(raw), max_read_bytes, path)
        return _error_response(
            "oversized_read",
            f"File exceeds max_read_bytes limit ({max_read_bytes} bytes)",
        )
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        _log.debug("read: binary content for %s", path)
        return _error_response("binary_content", "File content is not valid UTF-8")


async def _allocate_anchors(
    anchor_map: Any,
    session: Session,
    resolved: str,
    lines: list[str],
    *,
    start_index: int = 0,
) -> dict[int, str]:
    """Allocate anchor tokens for ``lines`` if ``anchor_map`` is not None.

    ``start_index`` is the file-absolute index of ``lines[0]``; callers that
    pass a tail slice must supply the correct offset.
    """
    if anchor_map is None:
        return {}
    file_meta = await session.stat(resolved)
    return anchor_map.allocate(resolved, file_meta.current_version_number, lines, start_index=start_index)


async def _check_size_before_read(
    session: Session,
    resource_limits: ResourceLimits,
    resolved: str,
) -> dict | None:
    """Pre-read size guard: return an oversized error dict before reading if stat shows too large.

    Returns ``None`` when no size limit is set or the file is within bounds.
    Returns a structured error dict when ``resource_limits.max_read_bytes`` is set and
    ``VersionMeta.size`` exceeds it, so the caller can return the error immediately
    without ever fetching the blob content.

    ``session.stat`` is called first (enforces read permission, raises NotFoundError when
    absent).  ``_meta.get_version`` is called to retrieve the size.  If the version
    record is absent the guard passes — the post-read check in ``_decode_raw`` still fires.
    """
    if resource_limits.max_read_bytes is None:
        return None
    file_meta = await session.stat(resolved)
    ver_meta = await session._vfs._meta.get_version(file_meta.namespace_id, file_meta.path)
    if ver_meta is not None and ver_meta.size > resource_limits.max_read_bytes:
        _log.debug(
            "stat: oversized (%d bytes > %d limit) for %s",
            ver_meta.size,
            resource_limits.max_read_bytes,
            resolved,
        )
        return _error_response(
            "oversized_read",
            f"File exceeds max_read_bytes limit ({resource_limits.max_read_bytes} bytes)",
        )
    return None


async def _build_ls_entry(fm: Any, session: Session, *, long: bool) -> dict:
    """Build a single ``ls`` entry dict from a ``FileMeta``.

    ``size`` is included only when ``long=True`` (fetched from ``VersionMeta``).
    ``is_dir`` is ``True`` when the path ends with ``/`` (synthetic directory prefix).
    """
    entry_path = fm.path
    is_dir = entry_path.endswith("/")
    name = posixpath.basename(entry_path.rstrip("/"))
    entry: dict = {
        "name": name,
        "path": entry_path,
        "is_dir": is_dir,
        "version_number": fm.current_version_number,
        "updated_at": fm.updated_at,
    }
    if long:
        ver = await session._vfs._meta.get_version(fm.namespace_id, fm.path)
        entry["size"] = ver.size if ver is not None else None
    return entry


def _truncated(items: list, max_result_items: int | None) -> tuple[list, bool]:
    """Return (possibly-sliced items, truncated_flag) capped to ``max_result_items``."""
    if max_result_items is not None and len(items) > max_result_items:
        return items[:max_result_items], True
    return items, False


# ---------------------------------------------------------------------------
# Shell-wrapper implementations (module-level; accept captured state explicitly)
# ---------------------------------------------------------------------------


async def _op_cd(session: Session, counter: OperationCounter, path: str) -> None:
    """cd: change cwd with budget enforcement."""
    counter.check_and_increment()
    await session.cd(path)


async def _op_pwd(session: Session, counter: OperationCounter) -> str:
    """pwd: return cwd with budget enforcement."""
    counter.check_and_increment()
    return session.pwd()


async def _op_cat(
    session: Session,
    counter: OperationCounter,
    resource_limits: ResourceLimits,
    anchor_map: Any,
    path: str,
) -> dict:
    r"""cat: read a file as strict UTF-8.

    Returns a dict with keys:

    - ``lines``: list of str (split on ``\n`` only; ``\r`` kept in content)
    - ``anchors``: dict mapping line index to anchor token (empty when
      ``anchor_map`` is None)
    - ``error``: None on success, or a dict with ``code`` and ``message`` on failure.

    Size check: ``_check_size_before_read`` stats the file first when
    ``max_read_bytes`` is set; if the stored size exceeds the limit the error
    is returned before the blob is fetched, preventing a host OOM.
    ``_decode_raw`` provides a belt-and-braces post-read size check.
    """
    counter.check_and_increment()
    resolved = resolve_path(session.pwd(), path)
    early_error = await _check_size_before_read(session, resource_limits, resolved)
    if early_error is not None:
        return early_error
    raw = await session.read(resolved)
    decoded = _decode_raw(raw, resource_limits.max_read_bytes, resolved)
    if isinstance(decoded, dict):
        return decoded
    lines = decoded.split("\n")
    anchors = await _allocate_anchors(anchor_map, session, resolved, lines)
    return {"lines": lines, "anchors": anchors, "error": None}


async def _op_head(
    session: Session,
    counter: OperationCounter,
    resource_limits: ResourceLimits,
    anchor_map: Any,
    path: str,
    n: int,
) -> dict:
    """head: return first ``n`` lines with optional anchor allocation.

    Stats before reading when ``max_read_bytes`` is set (same guard as ``cat``).
    """
    counter.check_and_increment()
    resolved = resolve_path(session.pwd(), path)
    early_error = await _check_size_before_read(session, resource_limits, resolved)
    if early_error is not None:
        return early_error
    raw = await session.read(resolved)
    decoded = _decode_raw(raw, resource_limits.max_read_bytes, resolved)
    if isinstance(decoded, dict):
        return decoded
    sliced = decoded.split("\n")[:n]
    anchors = await _allocate_anchors(anchor_map, session, resolved, sliced)
    return {"lines": sliced, "anchors": anchors, "error": None}


async def _op_tail(
    session: Session,
    counter: OperationCounter,
    resource_limits: ResourceLimits,
    anchor_map: Any,
    path: str,
    n: int,
) -> dict:
    """tail: return last ``n`` lines with optional anchor allocation.

    Anchor tokens are keyed by *file-absolute* line index, not slice-relative
    index.  For example, if a 6-line file is tailed with ``n=3``, the anchors
    returned are ``{3: tok3, 4: tok4, 5: tok5}`` — matching the actual position
    of each line in the file.  This is necessary so that ``edit()`` can use a
    tail anchor to target the correct line.

    Stats before reading when ``max_read_bytes`` is set (same guard as ``cat``).
    """
    counter.check_and_increment()
    resolved = resolve_path(session.pwd(), path)
    early_error = await _check_size_before_read(session, resource_limits, resolved)
    if early_error is not None:
        return early_error
    raw = await session.read(resolved)
    decoded = _decode_raw(raw, resource_limits.max_read_bytes, resolved)
    if isinstance(decoded, dict):
        return decoded
    all_lines = decoded.split("\n")
    sliced = all_lines[-n:] if n > 0 else []
    # File-absolute start index for the slice: tail of n lines on a total of L
    # starts at max(0, L - n).
    offset = max(0, len(all_lines) - n) if n > 0 else 0
    anchors = await _allocate_anchors(anchor_map, session, resolved, sliced, start_index=offset)
    return {"lines": sliced, "anchors": anchors, "error": None}


async def _op_ls(
    session: Session,
    counter: OperationCounter,
    max_result_items: int | None,
    path: str,
    *,
    long: bool = False,
) -> dict:
    """ls: list entries under ``path``.

    Returns a dict with keys:

    - ``entries``: list of dicts with ``name``, ``path``, ``is_dir``,
      ``version_number``, ``updated_at``; ``size`` added when ``long=True``.
    - ``truncated``: True when result count was capped by ``max_result_items``.

    Directory synthesis
    -------------------
    ``session.list`` returns file paths only; there are no explicit directory
    objects in the VFS.  To satisfy the spec's promise of synthesized ``is_dir``
    entries for implicit directory prefixes, ``_op_ls`` uses a recursive listing
    internally and then divides results into:

    - Depth-1 files (no additional ``/`` segment after the resolved prefix):
      built via ``_build_ls_entry`` as normal file entries (``is_dir=False``).
    - Deeper paths: one synthesized directory entry per distinct first segment,
      e.g. ``{"name": "sub", "path": "/src/sub/", "is_dir": True,
      "version_number": None, "updated_at": None}``.  Synthesized entries carry
      ``None`` for version/size/updated_at because they have no canonical version
      in the VFS.

    ``max_result_items`` is applied to the combined list (synthesized dirs first,
    then files — stable ordering).

    TODO(perf): the recursive listing is a correctness-first approach.  If ``ls``
    proves hot on large namespaces, add a dedicated ``list_immediate_children``
    store method that returns both file rows and distinct prefix segments in a
    single query.
    """
    counter.check_and_increment()
    resolved = resolve_path(session.pwd(), path)

    # Recursive listing to discover all descendant paths for directory synthesis.
    all_file_metas = await session.list(resolved, recursive=True)

    # Normalize prefix so suffix stripping always works (e.g. "/" → length 1).
    prefix = resolved if resolved.endswith("/") else resolved + "/"

    file_entries: list[dict] = []
    dir_entries: dict[str, dict] = {}  # dir_path → entry; insertion-ordered dedup

    for fm in all_file_metas:
        remainder = fm.path[len(prefix) :]
        slash_pos = remainder.find("/")
        if slash_pos == -1:
            # Depth-1 file — normal entry.
            file_entries.append(await _build_ls_entry(fm, session, long=long))
        else:
            # Deeper path — synthesize a dir entry for the immediate segment.
            seg = remainder[:slash_pos]
            dir_path = prefix + seg + "/"
            if dir_path not in dir_entries:
                dir_entries[dir_path] = {
                    "name": seg,
                    "path": dir_path,
                    "is_dir": True,
                    "version_number": None,
                    "updated_at": None,
                }

    # Synthesized dirs first (stable), then files.
    entries = list(dir_entries.values()) + file_entries
    entries, truncated = _truncated(entries, max_result_items)
    return {"entries": entries, "truncated": truncated}


async def _op_grep(
    session: Session,
    counter: OperationCounter,
    max_result_items: int | None,
    pattern: str,
    path: str,
    *,
    recursive: bool = True,
) -> dict:
    """grep: search for ``pattern`` (REGEX) under ``path``.

    When ``recursive=False`` results are filtered to depth-1 files only (files
    whose path has no additional ``/`` segment after the resolved scope).  The
    underlying ``session.search`` always scans recursively; non-recursive
    behaviour is achieved by post-filtering the results.

    Re-raises ``ReadBudgetExceededError``, ``ReindexRequiredError``, and
    ``IndexUnavailableError`` unchanged; they propagate to the sandbox.
    """
    counter.check_and_increment()
    resolved = resolve_path(session.pwd(), path)
    results = await session.search(pattern, resolved, SearchType.REGEX)
    items = [
        {"path": r.path, "line_number": r.line_number, "match_context": r.match_context, "score": r.score}
        for r in results
    ]
    if not recursive:
        # Filter to depth-1: no additional "/" segment after the scope prefix.
        prefix = resolved if resolved.endswith("/") else resolved + "/"
        items = [item for item in items if "/" not in item["path"][len(prefix) :]]
    items, truncated = _truncated(items, max_result_items)
    return {"results": items, "truncated": truncated}


async def _op_find(
    session: Session,
    counter: OperationCounter,
    max_result_items: int | None,
    path: str,
    **predicates: Any,
) -> dict:
    """find: search for files matching ``predicates`` under ``path`` (FIND).

    Accepted keyword args map to :class:`~vfs.protocols.search.FindPredicates`
    fields: ``name``, ``size_min``, ``size_max``, ``mtime_after``,
    ``mtime_before``, ``type``.
    """
    counter.check_and_increment()
    resolved = resolve_path(session.pwd(), path)
    find_preds = FindPredicates(**predicates)
    # ``DefaultSearchProvider._find_search`` uses the primary ``query`` as a
    # name-glob filter (Phase 1 behavior).  Use ``find_predicates.name`` as the
    # primary query so it acts as the name glob; fall back to ``"*"`` (match all)
    # when no name predicate is given.
    query = find_preds.name if find_preds.name is not None else "*"
    results = await session.search(query, resolved, SearchType.FIND, find_predicates=find_preds)
    items = [
        {"path": r.path, "line_number": r.line_number, "match_context": r.match_context, "score": r.score}
        for r in results
    ]
    items, truncated = _truncated(items, max_result_items)
    return {"results": items, "truncated": truncated}


async def _op_glob(
    session: Session,
    counter: OperationCounter,
    max_result_items: int | None,
    pattern: str,
) -> dict:
    """glob: return files matching ``pattern`` (GLOB) relative to cwd."""
    counter.check_and_increment()
    scope = session.pwd()
    results = await session.search(pattern, scope, SearchType.GLOB)
    items = [{"path": r.path, "score": r.score} for r in results]
    items, truncated = _truncated(items, max_result_items)
    return {"results": items, "truncated": truncated}


async def _op_write(
    session: Session,
    counter: OperationCounter,
    anchor_map: Any,
    path: str,
    content: bytes,
) -> Any:
    """write: write ``content`` to ``path`` and invalidate anchors.

    Returns a plain marshalable dict ``{"version_number": int, "size": int}``
    rather than the raw ``VersionMeta`` pydantic model.  Returning the model
    directly causes Monty to raise ``TypeError: Cannot convert VersionMeta to
    Monty value`` even when the sandbox discards the return value — and the
    write side-effect has already committed at that point.
    """
    counter.check_and_increment()
    resolved = resolve_path(session.pwd(), path)
    version_meta = await session.write(resolved, content)
    if anchor_map is not None:
        anchor_map.invalidate(resolved)
    return {"version_number": version_meta.version_number, "size": version_meta.size}


async def _op_read(session: Session, path: str, *, version_number: int | None = None) -> bytes:
    """read: raw byte read; no anchor allocation, no budget counter."""
    resolved = resolve_path(session.pwd(), path)
    return await session.read(resolved, version_number=version_number)


async def _op_stat(session: Session, path: str) -> Any:
    """stat: return FileMeta for path; no budget counter."""
    resolved = resolve_path(session.pwd(), path)
    return await session.stat(resolved)


async def _op_delete(session: Session, anchor_map: Any, path: str) -> Any:
    """delete: tombstone path and invalidate anchors; no budget counter (internal)."""
    resolved = resolve_path(session.pwd(), path)
    result = await session.delete(resolved)
    if anchor_map is not None:
        anchor_map.invalidate(resolved)
    return result


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def fs_operations_for(
    session: Session,
    resource_limits: ResourceLimits,
    anchor_map: Any | None = None,
) -> FsOperations:
    """Construct a session-bound :class:`FsOperations` with budget enforcement.

    All returned callables share a single :class:`OperationCounter` scoped to
    this factory call; separate calls produce independent counters.

    ``anchor_map`` is an optional :class:`~vfs.execution.anchors.AnchorMap`
    closed over by ``cat``/``head``/``tail`` and ``write``/``edit``.  When
    ``None`` (this revision), anchor allocation is skipped and ``write`` skips
    invalidation — designed so the anchored-editing chunk can plug in without
    changing this module.

    Parameters
    ----------
    session:
        The caller's :class:`~vfs.session.Session` (cwd + namespace + principal).
    resource_limits:
        Governs ``max_operations`` (shell-op budget), ``max_read_bytes`` (per-read
        content cap), and ``max_result_items`` (truncation cap for ls/grep/find).
    anchor_map:
        Optional :class:`~vfs.execution.anchors.AnchorMap`; ``None`` disables
        anchor tracking (cat/head/tail return plain line lists without tokens).
    """
    counter = OperationCounter(resource_limits.max_operations)
    mi = resource_limits.max_result_items

    async def _edit(
        path: str,
        start_anchor: str,
        end_anchor: str,
        replacement: list[str],
        *,
        expected_version: int | None = None,
    ) -> dict:
        """Anchor-validated edit per Decision (b).

        Steps:
        1. Resolve anchors — unknown token or path mismatch → AnchorConflictError.
        2. Stat pre-check: current version != anchor version → AnchorConflictError.
        3. Caller expected_version check: if supplied and differs from anchor's
           recorded version → AnchorConflictError (before any write).
        4. Read content; verify line at stored line_index equals line_content for
           both anchors; start must not be after end.
        5. Build replacement content; preserve trailing-newline presence.
        6. session.write with expected_version=anchor_version (CAS always applied).
           ConflictError / VersionCollisionError → AnchorConflictError (never retry).
        7. Reconcile anchors atomically; return structured result dict.
        """
        if anchor_map is None:
            raise AnchorConflictError("No anchor map available; cannot perform anchored edit.")

        counter.check_and_increment()
        resolved = resolve_path(session.pwd(), path)

        # Stage 1: resolve anchors (raises AnchorConflictError on unknown/wrong-path)
        anchor_version_start, line_content_start = anchor_map.validate(start_anchor, resolved)
        anchor_version_end, line_content_end = anchor_map.validate(end_anchor, resolved)

        # Both anchors must agree on version (they're from the same file)
        anchor_version = anchor_version_start
        if anchor_version_end != anchor_version:
            raise AnchorConflictError(
                f"Start anchor version {anchor_version_start} and end anchor version "
                f"{anchor_version_end} differ; re-read the file to obtain consistent anchors."
            )

        # Stage 1b: caller expected_version check (before stat — cheapest first)
        if expected_version is not None and expected_version != anchor_version:
            raise AnchorConflictError(
                f"Caller expected_version {expected_version} differs from anchor version "
                f"{anchor_version}; re-read the file to obtain fresh anchors."
            )

        # Stage 2: stat pre-check — current version must match anchor version
        file_meta = await session.stat(resolved)
        current_version = file_meta.current_version_number
        if current_version != anchor_version:
            raise AnchorConflictError(
                f"File {resolved!r} is at version {current_version}; anchor records version "
                f"{anchor_version}. Re-read the file to obtain fresh anchors."
            )

        # Retrieve the stored line indices for start/end anchors
        start_entry = anchor_map._entries[start_anchor]
        end_entry = anchor_map._entries[end_anchor]
        start_idx = start_entry.line_index
        end_idx = end_entry.line_index

        if start_idx > end_idx:
            raise AnchorConflictError(f"Start anchor line_index {start_idx} is after end anchor line_index {end_idx}.")

        # Stage 3: read file and verify line content for both anchors
        raw = await session.read(resolved)
        decoded = _decode_raw(raw, resource_limits.max_read_bytes, resolved)
        if isinstance(decoded, dict):
            # Binary or oversized — cannot edit
            raise AnchorConflictError(
                f"Cannot edit {resolved!r}: file content is not readable as UTF-8 or exceeds size limit."
            )
        old_lines = decoded.split("\n")

        # Verify start anchor line content
        if start_idx >= len(old_lines) or old_lines[start_idx] != line_content_start:
            raise AnchorConflictError(
                f"Line content at index {start_idx} has changed; re-read the file to obtain fresh anchors."
            )
        # Verify end anchor line content
        if end_idx >= len(old_lines) or old_lines[end_idx] != line_content_end:
            raise AnchorConflictError(
                f"Line content at index {end_idx} has changed; re-read the file to obtain fresh anchors."
            )

        # Stage 4: build replacement content
        # Preserve trailing-newline: if original content ended with \n, the last
        # element of old_lines is ""; we keep that invariant in new_lines.
        lines_before = old_lines[:start_idx]
        lines_after = old_lines[end_idx + 1 :]
        new_lines = lines_before + replacement + lines_after
        new_content = "\n".join(new_lines).encode("utf-8")

        # Stage 5: write with CAS on anchor_version
        try:
            version_meta = await session.write(resolved, new_content, expected_version=anchor_version)
        except (ConflictError, VersionCollisionError) as exc:
            raise AnchorConflictError(
                f"Write conflict on {resolved!r}; re-read the file to obtain fresh anchors."
            ) from exc

        new_version = version_meta.version_number

        # Stage 6: reconcile anchors atomically (no prior invalidate)
        new_idx_to_token = anchor_map.reconcile(resolved, old_lines, new_lines, new_version)

        # Build result: new version + anchors for the affected range + all updated anchors
        # Include anchors keyed by line index (consistent with cat return shape).
        return {
            "version_number": new_version,
            "anchors": new_idx_to_token,
            "lines": new_lines,
        }

    return FsOperations(
        cd=lambda path: _op_cd(session, counter, path),
        pwd=lambda: _op_pwd(session, counter),
        cat=lambda path: _op_cat(session, counter, resource_limits, anchor_map, path),
        head=lambda path, n: _op_head(session, counter, resource_limits, anchor_map, path, n),
        tail=lambda path, n: _op_tail(session, counter, resource_limits, anchor_map, path, n),
        ls=lambda path, **kw: _op_ls(session, counter, mi, path, **kw),
        grep=lambda pattern, path, **kw: _op_grep(session, counter, mi, pattern, path, **kw),
        find=lambda path, **kw: _op_find(session, counter, mi, path, **kw),
        glob=lambda pattern: _op_glob(session, counter, mi, pattern),
        write=lambda path, content: _op_write(session, counter, anchor_map, path, content),
        edit=_edit,
        read=lambda path, **kw: _op_read(session, path, **kw),
        stat=lambda path: _op_stat(session, path),
        delete=lambda path: _op_delete(session, anchor_map, path),
    )
