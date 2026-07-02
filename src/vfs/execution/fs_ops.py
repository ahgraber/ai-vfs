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

from vfs.errors import OperationBudgetExceededError
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

    All public fields are async callables corresponding to the ten shell
    wrappers (``cd``, ``pwd``, ``cat``, ``head``, ``tail``, ``ls``, ``grep``,
    ``find``, ``glob``, ``write``) plus internal fields (``read``,
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

    # Internal primitives (not part of the shell surface; used within the execution layer and tests)
    read: Any
    stat: Any
    delete: Any


# ---------------------------------------------------------------------------
# Module-level helpers (used both directly and by closure wrappers below)
# ---------------------------------------------------------------------------


def _error_response(code: str, message: str) -> dict:
    """Return a structured read-error dict with empty lines."""
    return {"lines": [], "error": {"code": code, "message": message}}


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
    path: str,
) -> dict:
    r"""cat: read a file as strict UTF-8.

    Returns a dict with keys:

    - ``lines``: list of str (split on ``\n`` only; ``\r`` kept in content)
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
    return {"lines": lines, "error": None}


async def _op_head(
    session: Session,
    counter: OperationCounter,
    resource_limits: ResourceLimits,
    path: str,
    n: int,
) -> dict:
    """head: return first ``n`` lines.

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
    sliced = decoded.split("\n")[:n] if n > 0 else []
    return {"lines": sliced, "error": None}


async def _op_tail(
    session: Session,
    counter: OperationCounter,
    resource_limits: ResourceLimits,
    path: str,
    n: int,
) -> dict:
    """tail: return last ``n`` lines.

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
    return {"lines": sliced, "error": None}


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

    # Scope the listing to the directory prefix (trailing slash) so a bare
    # "/proj" does not bleed into sibling prefixes like "/projector/…" — a
    # ``LIKE '/proj%'`` scan would over-match. The slashed prefix also drives the
    # suffix stripping below (e.g. "/" → length 1).
    prefix = resolved if resolved.endswith("/") else resolved + "/"

    # Recursive listing to discover all descendant paths for directory synthesis.
    all_file_metas = await session.list(prefix, recursive=True)

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
    # Scope the search to the directory prefix (trailing slash) so a bare "/proj"
    # does not bleed into sibling prefixes like "/projector/…".
    prefix = resolved if resolved.endswith("/") else resolved + "/"
    results = await session.search(pattern, prefix, SearchType.REGEX)
    items = [
        {"path": r.path, "line_number": r.line_number, "match_context": r.match_context, "score": r.score}
        for r in results
    ]
    if not recursive:
        # Filter to depth-1: no additional "/" segment after the scope prefix.
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
    resource_limits: ResourceLimits,
    path: str,
    content: bytes,
) -> Any:
    """write: write ``content`` to ``path``.

    Returns a plain marshalable dict ``{"version_number": int, "size": int,
    "error": None}`` rather than the raw ``VersionMeta`` pydantic model.  Returning
    the model directly causes Monty to raise ``TypeError: Cannot convert VersionMeta
    to Monty value`` even when the sandbox discards the return value — and the write
    side-effect has already committed at that point.

    Refuses with a structured ``oversized_write`` error (``version_number`` ``None``,
    nothing written) when ``content`` exceeds ``resource_limits.max_write_bytes`` —
    the injected-verb counterpart of the native-mount write cap, so sandboxed code
    cannot bypass ``max_write_bytes`` by calling the injected ``write``.
    """
    counter.check_and_increment()
    if resource_limits.max_write_bytes is not None and len(content) > resource_limits.max_write_bytes:
        _log.debug(
            "write: oversized (%d bytes > %d limit) for %s", len(content), resource_limits.max_write_bytes, path
        )
        return {
            "version_number": None,
            "size": 0,
            "error": {
                "code": "oversized_write",
                "message": f"Write exceeds max_write_bytes limit ({resource_limits.max_write_bytes} bytes)",
            },
        }
    resolved = resolve_path(session.pwd(), path)
    version_meta = await session.write(resolved, content)
    return {"version_number": version_meta.version_number, "size": version_meta.size, "error": None}


async def _op_read(session: Session, path: str, *, version_number: int | None = None) -> bytes:
    """read: raw byte read; no budget counter."""
    resolved = resolve_path(session.pwd(), path)
    return await session.read(resolved, version_number=version_number)


async def _op_stat(session: Session, path: str) -> Any:
    """stat: return FileMeta for path; no budget counter."""
    resolved = resolve_path(session.pwd(), path)
    return await session.stat(resolved)


async def _op_delete(session: Session, path: str) -> Any:
    """delete: tombstone path; no budget counter (internal)."""
    resolved = resolve_path(session.pwd(), path)
    return await session.delete(resolved)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def fs_operations_for(
    session: Session,
    resource_limits: ResourceLimits,
    counter: OperationCounter | None = None,
) -> FsOperations:
    """Construct a session-bound :class:`FsOperations` with budget enforcement.

    Parameters
    ----------
    session:
        The caller's :class:`~vfs.session.Session` (cwd + namespace + principal).
    resource_limits:
        Governs ``max_operations`` (shell-op budget), ``max_read_bytes`` (per-read
        content cap), and ``max_result_items`` (truncation cap for ls/grep/find).
    counter:
        Shared :class:`OperationCounter` to charge each operation against.  When
        ``None`` a fresh counter scoped to this factory call is created.  Pass the
        same counter to :class:`~vfs.execution.fs_port.SessionFsPort` so the
        injected verbs and the native-mount file operations draw from one budget.
    """
    if counter is None:
        counter = OperationCounter(resource_limits.max_operations)
    mi = resource_limits.max_result_items

    return FsOperations(
        cd=lambda path: _op_cd(session, counter, path),
        pwd=lambda: _op_pwd(session, counter),
        cat=lambda path: _op_cat(session, counter, resource_limits, path),
        head=lambda path, n: _op_head(session, counter, resource_limits, path, n),
        tail=lambda path, n: _op_tail(session, counter, resource_limits, path, n),
        ls=lambda path, **kw: _op_ls(session, counter, mi, path, **kw),
        grep=lambda pattern, path, **kw: _op_grep(session, counter, mi, pattern, path, **kw),
        find=lambda path, **kw: _op_find(session, counter, mi, path, **kw),
        glob=lambda pattern: _op_glob(session, counter, mi, pattern),
        write=lambda path, content: _op_write(session, counter, resource_limits, path, content),
        read=lambda path, **kw: _op_read(session, path, **kw),
        stat=lambda path: _op_stat(session, path),
        delete=lambda path: _op_delete(session, path),
    )
