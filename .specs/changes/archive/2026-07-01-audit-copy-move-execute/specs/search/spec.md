# Search — Delta Spec

> Change: `audit-copy-move-execute`
> Date: 2026-06-27

## MODIFIED Requirements

### Requirement: RegexContentSearch

> Previously: regex content search was specified only as "match against file content, returning matching paths with context", with no engine guarantee — the implementation used Python's backtracking `re` engine on the host event loop, where an adversarial pattern could exhibit catastrophic backtracking, and PostgreSQL applied a whole-document anchor-sensitive `~` prune that could differ from per-line matching.

The system SHALL support regex pattern matching against file content,
returning matching paths with context (matched line and line number).

Patterns SHALL be matched line-by-line with a **linear-time** engine (RE2), so `^`/`$` anchor to line bounds and no pattern can exhibit catastrophic (super-linear) backtracking — regex content search is reachable by untrusted sandboxed code via `grep`, so an adversarial pattern MUST NOT be able to wedge the host event loop.
Consequently, patterns using features RE2 does not implement (backreferences, lookaround) SHALL be treated as unusable and yield an empty result set rather than raising or falling back to a backtracking engine.
This engine SHALL be used uniformly across every backend's in-process verification, so REGEX results are identical across backends (no backend applies a whole-document anchor-sensitive prune that could differ from per-line matching).

Serves: dos-resistant-search

#### Scenario: GrepMatchesContent

- **GIVEN** file /src/main.py contains "# TODO: fix this" on line 3
- **WHEN** a principal searches with regex "TODO" in scope /src/
- **THEN** a result with path=/src/main.py, line_number=3, and match_context containing "fix this" is returned

#### Scenario: GrepNoMatch

- **GIVEN** no files in scope contain the pattern
- **WHEN** a principal searches with regex "NONEXISTENT"
- **THEN** an empty result list is returned

#### Scenario: RegexIsLinearTime

- **GIVEN** an adversarial catastrophic-backtracking pattern such as `(a+)+$` and content that does not match it
- **WHEN** a principal (or sandboxed `grep`) searches with that pattern
- **THEN** the search completes in linear time without blocking, returning no match — it does not hang
