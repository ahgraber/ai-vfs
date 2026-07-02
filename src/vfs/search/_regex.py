"""Linear-time regex compilation for content search.

Content search compiles patterns that, in the sandboxed-execution path, are
supplied by *untrusted* agent code (via ``grep``). Python's ``re`` uses a
backtracking engine that can exhibit catastrophic (super-linear) blowup on
adversarial patterns such as ``(a+)+$``. The per-line verification loop runs
synchronously, so a single such pattern wedges the host event loop past any
``asyncio.wait_for`` deadline (a synchronous CPU loop has no ``await`` point at
which cancellation can take effect) — a denial-of-service vector.

RE2 evaluates in guaranteed linear time with no backtracking, closing that
vector at the source. Patterns using features RE2 does not implement
(backreferences, lookaround) raise :class:`RegexCompileError` rather than
silently falling back to the vulnerable engine.

Line-oriented use
-----------------
All callers verify one text line at a time, so ``^``/``$`` anchor to line
boundaries (the string bounds of each line) — the intended grep semantics, and
consistent across every backend that uses this helper.
"""

from __future__ import annotations

from typing import Protocol

import re2

# RE2 logs parse errors to stderr via absl by default; suppress that so an
# invalid untrusted pattern cannot spam host logs.
_OPTIONS = re2.Options()
_OPTIONS.log_errors = False


class RegexCompileError(ValueError):
    """Raised when a pattern is invalid or uses features RE2 does not support."""


class CompiledLineRegex(Protocol):
    """The subset of the compiled-regex API the search paths rely on."""

    def search(self, text: str) -> object | None:
        """Return a truthy match object if ``text`` contains a match, else ``None``."""
        ...


def compile_line_regex(pattern: str) -> CompiledLineRegex:
    """Compile ``pattern`` with the linear-time RE2 engine.

    Raises :class:`RegexCompileError` for invalid or unsupported syntax so
    callers can treat an unusable pattern as "no matches" uniformly.
    """
    try:
        return re2.compile(pattern, options=_OPTIONS)
    except re2.error as exc:
        raise RegexCompileError(str(exc)) from exc
