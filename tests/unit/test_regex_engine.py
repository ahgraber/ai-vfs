"""Unit tests for the linear-time (RE2) content-regex engine.

Guards the ReDoS fix: agent-supplied ``grep`` patterns are compiled with RE2, so
catastrophic-backtracking patterns cannot wedge the synchronous verification loop
(and therefore cannot defeat ``vfs.execute``'s wall-clock timeout). Also pins the
cross-backend contract that an unsupported/invalid pattern yields "no matches"
rather than raising.
"""

from __future__ import annotations

import time

import pytest

from vfs.search._regex import RegexCompileError, compile_line_regex


def test_compiles_and_matches_standard_pattern() -> None:
    compiled = compile_line_regex(r"foo\d+")
    assert compiled.search("xx foo123 yy")
    assert not compiled.search("no digits here")


def test_anchors_bind_to_line_bounds() -> None:
    # Callers verify one line at a time, so ^/$ anchor to the line's bounds.
    compiled = compile_line_regex(r"^import\b")
    assert compiled.search("import os")
    assert not compiled.search("    import os")


def test_catastrophic_pattern_is_linear_time() -> None:
    # Under Python's backtracking ``re`` this pattern against a long non-matching
    # line is super-linear (seconds to effectively forever). RE2 evaluates it in
    # linear time; assert it returns near-instantly so a hang is a hard failure.
    compiled = compile_line_regex(r"(a+)+$")
    line = "a" * 5000 + "!"
    start = time.perf_counter()
    result = compiled.search(line)
    elapsed = time.perf_counter() - start
    assert result is None
    assert elapsed < 0.5, f"regex took {elapsed:.3f}s — engine is not linear-time"


@pytest.mark.parametrize(
    "pattern",
    [
        r"(a)\1",  # backreference
        r"(?=foo)bar",  # lookahead
        r"(?<=x)y",  # lookbehind
        r"(unterminated",  # invalid syntax
    ],
)
def test_unsupported_or_invalid_syntax_raises(pattern: str) -> None:
    with pytest.raises(RegexCompileError):
        compile_line_regex(pattern)
