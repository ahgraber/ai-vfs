"""AnchoredEditor — the stateless anchored-editing surface over a Session.

``read_anchored`` returns a window of a file's lines, the file's current
version, and a per-line anchor for each returned line (absolute indices).
``edit_anchored`` applies one or more hunks atomically under a strict version
check, writing a single new version, and returns success + the new version
only — never content or anchors.

The surface is bound to a ``(namespace, principal)`` context via a
:class:`~vfs.session.Session`, so it works across independent calls without an
execution sandbox, and every operation enforces the principal's permissions.
The same capability backs both the standalone tool an agent framework calls and
the in-sandbox ``edit`` verb.
"""

from __future__ import annotations

from dataclasses import dataclass

from vfs.anchored_editing.anchors import K_DEFAULT, anchors_for_lines, resolve_anchor
from vfs.errors import AnchorConflictError, ConflictError, ContentDecodeError, NotFoundError, VersionCollisionError
from vfs.session import Session, resolve_path


@dataclass(frozen=True)
class AnchoredReadResult:
    r"""Result of ``read_anchored``.

    ``lines`` are the window's lines (split on ``\n`` only, ``\r`` retained).
    ``anchors`` maps each line's **absolute** file index to its anchor token.
    ``offset`` is the absolute index of ``lines[0]``.
    """

    version: int
    lines: list[str]
    anchors: dict[int, str]
    offset: int


@dataclass(frozen=True)
class Hunk:
    """One replacement: lines ``start_anchor``..``end_anchor`` (inclusive) → ``replacement``."""

    start_anchor: str
    end_anchor: str
    replacement: list[str]


@dataclass(frozen=True)
class AnchoredEditResult:
    """Agent-facing result of ``edit_anchored``: success and the new version only."""

    new_version: int


def _decode(raw: bytes) -> str:
    """Decode ``raw`` as strict UTF-8; raise :class:`ContentDecodeError` otherwise."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContentDecodeError("File content is not valid UTF-8; cannot anchor.") from exc


class AnchoredEditor:
    """Stateless anchored-editing surface bound to a single ``Session``."""

    def __init__(self, session: Session, *, k: int = K_DEFAULT) -> None:
        self._session = session
        self._k = k

    async def read_anchored(
        self,
        path: str,
        offset: int | None = None,
        limit: int | None = None,
    ) -> AnchoredReadResult:
        r"""Return content (whole file, or the ``offset``/``limit`` window), version, and anchors.

        Content is decoded as strict UTF-8 (undecodable → :class:`ContentDecodeError`,
        no anchors). Lines split on ``\n`` only; ``\r`` and trailing-newline
        presence preserved. Anchor indices are file-absolute, identical for a full
        or windowed read. Enforces the principal's read permission.
        """
        resolved = resolve_path(self._session.pwd(), path)
        file_meta = await self._session.stat(resolved)
        raw = await self._session.read(resolved)
        text = _decode(raw)
        lines = text.split("\n")

        start = offset or 0
        window = lines[start:] if limit is None else lines[start : start + limit]
        anchors = anchors_for_lines(window, start_index=start, k=self._k)
        return AnchoredReadResult(
            version=file_meta.current_version_number,
            lines=window,
            anchors=anchors,
            offset=start,
        )

    async def edit_anchored(
        self,
        path: str,
        hunks: list[Hunk],
        expected_version: int,
    ) -> AnchoredEditResult:
        """Apply ``hunks`` atomically when the file is still at ``expected_version``.

        Conflicts (and writes nothing) when: the current version differs from
        ``expected_version``; any anchor's checksum mismatches, is out of range,
        or is malformed; a hunk is inverted; hunks overlap; or the path is a
        tombstone. The strict version check runs before any write, and the CAS
        write is the authoritative guard (correctness rests on single-record CAS,
        not read freshness). Enforces the principal's write permission.
        """
        if not hunks:
            raise AnchorConflictError("edit_anchored requires at least one hunk.")

        resolved = resolve_path(self._session.pwd(), path)

        # Strict version check before any write (also enforces read permission).
        file_meta = await self._session.stat(resolved)
        if file_meta.is_deleted:
            raise NotFoundError(f"File not found (tombstoned): {resolved}")
        if file_meta.current_version_number != expected_version:
            raise AnchorConflictError(
                f"File {resolved!r} is at version {file_meta.current_version_number}; "
                f"edit expected version {expected_version}. Re-read the file to obtain fresh anchors."
            )

        # Read the exact content the anchors came from (deterministic resolution).
        raw = await self._session.read(resolved, version_number=expected_version)
        lines = _decode(raw).split("\n")

        # Resolve every hunk against that content; any failure aborts the whole edit.
        resolved_hunks: list[tuple[int, int, list[str]]] = []
        for hunk in hunks:
            start_idx = resolve_anchor(hunk.start_anchor, lines)
            end_idx = resolve_anchor(hunk.end_anchor, lines)
            if end_idx < start_idx:
                raise AnchorConflictError(
                    f"Hunk end anchor (index {end_idx}) resolves before start anchor (index {start_idx})."
                )
            resolved_hunks.append((start_idx, end_idx, hunk.replacement))

        # Apply non-overlapping hunks left-to-right; reject overlap.
        resolved_hunks.sort(key=lambda h: h[0])
        for (_, prev_end, _), (cur_start, _, _) in zip(resolved_hunks, resolved_hunks[1:]):
            if cur_start <= prev_end:
                raise AnchorConflictError(
                    f"Overlapping hunks: a hunk starting at index {cur_start} overlaps one ending at {prev_end}."
                )

        new_lines: list[str] = []
        cursor = 0
        for start_idx, end_idx, replacement in resolved_hunks:
            new_lines.extend(lines[cursor:start_idx])
            new_lines.extend(replacement)
            cursor = end_idx + 1
        new_lines.extend(lines[cursor:])
        new_content = "\n".join(new_lines).encode("utf-8")

        # CAS write — the authoritative concurrency guard.
        try:
            version_meta = await self._session.write(resolved, new_content, expected_version=expected_version)
        except (ConflictError, VersionCollisionError) as exc:
            raise AnchorConflictError(
                f"Write conflict on {resolved!r}; the file changed concurrently. Re-read to obtain fresh anchors."
            ) from exc

        return AnchoredEditResult(new_version=version_meta.version_number)
