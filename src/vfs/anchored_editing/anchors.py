"""Stateless content-derived anchors for line-precise editing.

An anchor identifies a single line by its **absolute (file-relative) index**
together with a short checksum bound to that line's ``(index, content)`` pair,
rendered ``{index}:{checksum}`` (e.g. ``47:9c2``).

- The **index carries identity**: under strict edit conflict (any change since
  the read is a conflict), ``version == expected_version`` guarantees
  byte-identical content, so the literal index targets exactly one line — with
  probability 1, including lines whose text is identical to other lines
  (blank lines, repeated boilerplate).
- The **checksum is an integrity/fabrication + proof-of-read guard**, not a
  locator. Binding it to ``(index, content)`` catches an index transposed
  between two identical lines, an anchor pasted from a different file, and an
  in-range hallucinated index — all of which would otherwise silently edit the
  wrong line.

Anchors are reproducible purely from file content — no server-side map, no
lifetime to manage. The functions here are pure (no I/O).
"""

from __future__ import annotations

import blake3

from vfs.errors import AnchorConflictError

#: Default checksum length in hex characters. ``k=3`` (1/4096) is chosen because
#: the checksum doubles as a proof-of-read guard (an agent editing a line it
#: never displayed must guess the per-line tag); ``k=2`` would suffice if it were
#: only a version-CAS backstop. Tunable; the index alone bears uniqueness.
K_DEFAULT = 3


def checksum(abs_line_index: int, line_text: str, *, k: int = K_DEFAULT) -> str:
    """Return the ``k``-hex-char checksum binding ``abs_line_index`` to ``line_text``.

    Hashes ``(index ⊕ content)`` rather than content alone so that transposing
    an index between two identical lines is detectable: ``47`` and ``48`` of the
    same text produce different checksums.
    """
    digest = blake3.blake3(f"{abs_line_index}\n{line_text}".encode()).hexdigest()
    return digest[:k]


def make_anchor(abs_line_index: int, line_text: str, *, k: int = K_DEFAULT) -> str:
    """Return the anchor token ``{abs_line_index}:{checksum}`` for one line."""
    return f"{abs_line_index}:{checksum(abs_line_index, line_text, k=k)}"


def anchors_for_lines(lines: list[str], *, start_index: int = 0, k: int = K_DEFAULT) -> dict[int, str]:
    """Return ``{absolute_index: anchor}`` for ``lines`` starting at ``start_index``.

    ``start_index`` is the file-absolute index of ``lines[0]``; callers that pass
    a window (e.g. a tail slice) supply the correct offset so anchor indices are
    file-absolute, not window-relative.
    """
    return {start_index + offset: make_anchor(start_index + offset, line, k=k) for offset, line in enumerate(lines)}


def parse_anchor(anchor: str) -> tuple[int, str]:
    """Parse an anchor token into ``(absolute_index, checksum)``.

    Raises :class:`~vfs.errors.AnchorConflictError` when the token is not of the
    form ``{non-negative-int}:{non-empty-checksum}``.
    """
    if not isinstance(anchor, str) or ":" not in anchor:
        raise AnchorConflictError(f"Malformed anchor {anchor!r}; expected '<index>:<checksum>'.")
    index_str, _, ck = anchor.partition(":")
    if not index_str.isdigit() or not ck:
        raise AnchorConflictError(f"Malformed anchor {anchor!r}; expected '<index>:<checksum>'.")
    return int(index_str), ck


def resolve_anchor(anchor: str, lines: list[str]) -> int:
    """Resolve ``anchor`` against ``lines`` and return its absolute line index.

    Verifies the index is in range and the checksum matches the content at that
    index (using the anchor's own checksum length). Raises
    :class:`~vfs.errors.AnchorConflictError` on a malformed token, an
    out-of-range index, or a checksum mismatch (fabrication / transposition /
    cross-file paste).
    """
    index, ck = parse_anchor(anchor)
    if index < 0 or index >= len(lines):
        raise AnchorConflictError(
            f"Anchor {anchor!r} index {index} is out of range (file has {len(lines)} lines); re-read the file."
        )
    expected = checksum(index, lines[index], k=len(ck))
    if ck != expected:
        raise AnchorConflictError(
            f"Anchor {anchor!r} checksum does not match content at index {index}; "
            "the line changed, the index was transposed, or the anchor is from another file. Re-read the file."
        )
    return index
