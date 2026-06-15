"""AnchorMap — session-scoped anchor state for token-efficient editing.

An ``AnchorMap`` is constructed inside ``fs_operations_for`` and lives for
exactly one ``execute`` call.  Anchors bind a short token to
``(path, version_number, line_index, line_content)`` so agents can reference
specific lines without re-reading the whole file.

Pool design
-----------
The single-token pool consists of short ASCII identifier-safe strings that are
plausibly single-token across common BPE tokenizers (best-effort;
tokenizer-dependent).  This follows the rationale from the Dirac project, which
curated ~1,700 anchors for the o200k_base vocabulary.

Concretely, the pool contains:

1. All 676 two-character lowercase-ASCII combinations (``aa`` … ``zz``),
   generated as ``string.ascii_lowercase × string.ascii_lowercase``.
2. The first 1,024 three-character lowercase-ASCII combinations in the same
   lexicographic order (``aaa``, ``aab``, … up to index 1023), giving a total
   pool size of 1,700 entries — matching the Dirac reference count.

Two- and three-char lowercase sequences are among the most stable single-token
strings in BPE vocabularies: they appear as common morphemes and abbreviations
and are present as discrete tokens in all major publicly available vocabularies.

When the pool is exhausted the allocator falls back to random 2–4 character
alphanumeric strings that do not collide with pool entries or already-issued
tokens.
"""

from __future__ import annotations

import difflib
import random
import string

from vfs.errors import AnchorConflictError

# ---------------------------------------------------------------------------
# Pool construction
# ---------------------------------------------------------------------------

_ALPHA = string.ascii_lowercase  # 'a'…'z'

# 676 two-char entries
_TWO_CHAR = [a + b for a in _ALPHA for b in _ALPHA]

# First 1024 three-char entries (lexicographic order)
_THREE_CHAR_GEN = (a + b + c for a in _ALPHA for b in _ALPHA for c in _ALPHA)
_THREE_CHAR = [next(_THREE_CHAR_GEN) for _ in range(1024)]

# Full pool: 676 + 1024 = 1700 entries
_POOL: list[str] = _TWO_CHAR + _THREE_CHAR

# Fast lookup for collision avoidance in the fallback allocator
_POOL_SET: frozenset[str] = frozenset(_POOL)


# ---------------------------------------------------------------------------
# Internal entry type
# ---------------------------------------------------------------------------


class _AnchorEntry:
    """Internal record for one allocated anchor."""

    __slots__ = ("path", "version_number", "line_index", "line_content")

    def __init__(self, path: str, version_number: int, line_index: int, line_content: str) -> None:
        self.path = path
        self.version_number = version_number
        self.line_index = line_index
        self.line_content = line_content


# ---------------------------------------------------------------------------
# AnchorMap
# ---------------------------------------------------------------------------


class AnchorMap:
    """Session-scoped anchor registry.

    Lifetime: one ``execute`` call (one ``fs_operations_for`` invocation).

    Tokens are allocated from a fixed single-token pool on first use; when the
    pool is exhausted the allocator falls back to short (2–4 character) random
    alphanumeric strings that do not collide with pool entries or already-issued
    tokens.

    Each entry binds ``(path, version_number, line_index, line_content)``.

    Validation checks:
    1. Token must be known and bound to ``path`` (raises ``AnchorConflictError``
       on mismatch or unknown token).
    2. The file's current version number must match the recorded ``version_number``
       (checked by the caller via ``session.stat`` before calling ``validate``).
    3. The line at ``line_index`` must equal ``line_content`` (checked by the
       caller after reading the file).

    ``invalidate(path)`` drops all entries for ``path`` — called by raw
    ``write``/``delete`` through ``FsOperations``.

    ``reconcile(path, old_lines, new_lines, version_number)`` atomically replaces
    the path's anchor state after a successful ``edit()``: unchanged lines keep
    their tokens with updated ``line_index`` and the new ``version_number``;
    changed/inserted lines receive new tokens; dropped lines are removed.
    """

    def __init__(self) -> None:
        # token -> _AnchorEntry
        self._entries: dict[str, _AnchorEntry] = {}
        # index into _POOL for next allocation
        self._pool_idx: int = 0
        # set of already-issued tokens (pool + fallback) for collision avoidance
        self._issued: set[str] = set()

    # ------------------------------------------------------------------
    # Token allocation
    # ------------------------------------------------------------------

    def _next_token(self) -> str:
        """Return the next available token (pool first, then random fallback)."""
        if self._pool_idx < len(_POOL):
            tok = _POOL[self._pool_idx]
            self._pool_idx += 1
            self._issued.add(tok)
            return tok
        # Fallback: random 2–4 char alphanumeric string not in pool or already issued.
        chars = string.ascii_letters + string.digits
        while True:
            length = random.randint(2, 4)  # noqa: S311 — non-cryptographic, intentional
            tok = "".join(random.choices(chars, k=length))  # noqa: S311
            if tok not in _POOL_SET and tok not in self._issued:
                self._issued.add(tok)
                return tok

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def allocate(
        self,
        path: str,
        version_number: int,
        lines: list[str],
        *,
        start_index: int = 0,
    ) -> dict[int, str]:
        """Allocate anchor tokens for ``lines`` and bind them to ``path``.

        Returns a dict mapping ``line_index`` → ``anchor_token`` for every line,
        where ``line_index`` is the file-absolute index (``start_index + offset``).

        ``start_index`` is the file-absolute index of ``lines[0]``; callers that
        pass a slice of the full file (e.g. ``tail``) must supply the correct
        offset so that anchor ``line_index`` values match the actual position in
        the file.  ``head`` and ``cat`` always start at 0 (the default).

        First allocations use pool tokens; subsequent calls after pool exhaustion
        use random fallback tokens.

        Existing entries for ``path`` are NOT cleared; new tokens are added.
        Use ``invalidate(path)`` first if you want a clean slate (e.g. after
        a raw write — but ``edit()`` uses ``reconcile``, not
        ``invalidate`` + ``allocate``).
        """
        result: dict[int, str] = {}
        for offset, line in enumerate(lines):
            idx = start_index + offset
            tok = self._next_token()
            self._entries[tok] = _AnchorEntry(
                path=path,
                version_number=version_number,
                line_index=idx,
                line_content=line,
            )
            result[idx] = tok
        return result

    def validate(self, anchor_token: str, path: str) -> tuple[int, str]:
        """Return ``(version_number, line_content)`` for a known, path-matching anchor.

        Raises ``AnchorConflictError`` when:
        - The token is unknown (never allocated, or invalidated).
        - The token is bound to a different path.

        The caller is responsible for the subsequent version-number and
        line-content checks (which require a ``stat`` and a file read).
        """
        entry = self._entries.get(anchor_token)
        if entry is None:
            raise AnchorConflictError(
                f"Unknown anchor token {anchor_token!r}; re-read the file to obtain fresh anchors."
            )
        if entry.path != path:
            raise AnchorConflictError(f"Anchor {anchor_token!r} is bound to {entry.path!r}, not {path!r}.")
        return entry.version_number, entry.line_content

    def invalidate(self, path: str) -> None:
        """Drop all anchor entries for ``path``.

        Called after a raw ``write`` or ``delete`` through ``FsOperations``.
        After invalidation any attempt to ``validate`` an old token for ``path``
        raises ``AnchorConflictError``.
        """
        to_remove = [tok for tok, entry in self._entries.items() if entry.path == path]
        for tok in to_remove:
            del self._entries[tok]

    def reconcile(
        self,
        path: str,
        old_lines: list[str],
        new_lines: list[str],
        version_number: int,
    ) -> dict[int, str]:
        """Atomically replace anchor state for ``path`` after a successful ``edit()``.

        Runs a longest-common-block (difflib) reconciliation via
        ``difflib.SequenceMatcher`` between ``old_lines`` and ``new_lines``:
        - Unchanged lines keep their existing tokens; their ``line_index`` is
          updated to the new position and ``version_number`` is updated.
        - Changed or inserted lines receive new tokens from the pool.
        - Deleted lines are removed from the map.

        **Positional caveat:** ``difflib.SequenceMatcher`` uses Ratcliff/Obershelp
        heuristics to find longest common blocks.  When a file contains duplicate
        line content, the matcher may assign the same-content line's existing token
        to a *different* positional occurrence in the new file.  This is correct
        behaviour — the token follows the content match, not a fixed position.
        Callers should not rely on a specific positional token assignment for
        duplicate-content lines.

        The path's anchor state is replaced atomically: the old entries are
        removed and the new ones installed in a single operation, so no
        intermediate state is ever visible.

        Returns the new ``line_index`` → ``token`` mapping for ``path``.
        """
        # Collect ALL tokens bound to this path (not just the last-wins per line_index).
        # When allocate() was called multiple times for the same path (e.g. tail then cat),
        # duplicate tokens sharing a line_index are present; the reverse-index dict only
        # keeps one per index, leaving the others as orphans unless we track all tokens here.
        all_path_tokens: list[str] = [tok for tok, entry in self._entries.items() if entry.path == path]

        # Build reverse index: old_line_index -> token (for this path only).
        # When two tokens share the same line_index, the last one wins (arbitrary but
        # deterministic); all tokens are removed in the atomic-replacement step below.
        old_idx_to_token: dict[int, str] = {
            entry.line_index: tok for tok, entry in self._entries.items() if entry.path == path
        }

        # Compute diff opcodes using SequenceMatcher (Ratcliff/Obershelp longest-common-block;
        # stdlib, no extra dependency).
        matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)

        new_entries: dict[str, _AnchorEntry] = {}
        new_idx_to_token: dict[int, str] = {}

        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                # Unchanged lines — reuse existing tokens, update position + version.
                for old_pos, new_pos in zip(range(i1, i2), range(j1, j2)):
                    tok = old_idx_to_token.get(old_pos)
                    if tok is None:
                        # No prior token for this position (e.g. first allocate
                        # covered only a slice); allocate a new one.
                        tok = self._next_token()
                    new_entries[tok] = _AnchorEntry(
                        path=path,
                        version_number=version_number,
                        line_index=new_pos,
                        line_content=new_lines[new_pos],
                    )
                    new_idx_to_token[new_pos] = tok
            elif tag in ("replace", "insert"):
                # Changed or inserted lines — allocate new tokens.
                for new_pos in range(j1, j2):
                    tok = self._next_token()
                    new_entries[tok] = _AnchorEntry(
                        path=path,
                        version_number=version_number,
                        line_index=new_pos,
                        line_content=new_lines[new_pos],
                    )
                    new_idx_to_token[new_pos] = tok
            # tag == "delete": old lines removed; their tokens are dropped (not added to new_entries).

        # Atomic replacement: remove ALL old entries for this path (including duplicates
        # from overlapping allocations), then install the reconciled set.
        for tok in all_path_tokens:
            self._entries.pop(tok, None)
        self._entries.update(new_entries)

        return new_idx_to_token
