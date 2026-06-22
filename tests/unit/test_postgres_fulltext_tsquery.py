"""Static unit tests for the Postgres fulltext tsquery construction.

These exercise ``_build_fulltext_tsquery`` (the dynamic-SQL builder behind the Postgres
``NativeTextSearch`` fulltext path) without touching a database.  They guard the invariants
that matter for safety and correctness:

- user terms are carried only as separate bound parameters (never interpolated into SQL),
- the ANY fragment OR-combines one ``plainto_tsquery`` per whitespace-split term,
- a single-term ANY query reduces to exactly one ``plainto_tsquery`` call (identical to ALL),
- an empty/whitespace-only query yields ``None`` so the caller returns an empty response,
- the fragment compiles as valid Postgres SQL when reused in both the score and predicate.

The module imports without ``asyncpg`` (engine creation is deferred), so these run in the
default suite.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from vfs.models import FullTextMatchMode
from vfs.stores.postgres_metadata import _build_fulltext_tsquery


def _compile(fragment: str, binds: dict[str, str]):
    """Compile the fragment in both score and predicate positions against the PG dialect."""
    stmt = sa.text(
        f"SELECT ts_rank(to_tsvector('simple', raw_text), {fragment}) "
        f"WHERE to_tsvector('simple', raw_text) @@ ({fragment})"
    ).bindparams(**binds)
    return stmt.compile(dialect=postgresql.dialect())


def test_all_mode_single_plainto_tsquery() -> None:
    """ALL mode uses a single plainto_tsquery over the whole query bound to :query."""
    built = _build_fulltext_tsquery("hello cloud", FullTextMatchMode.ALL)
    assert built is not None
    fragment, binds = built
    assert fragment == "plainto_tsquery('simple', :query)"
    assert binds == {"query": "hello cloud"}
    _compile(fragment, binds)  # must not raise


def test_any_mode_or_combines_per_term() -> None:
    """ANY mode emits one plainto_tsquery per term, OR-combined with ||, one bind per term."""
    built = _build_fulltext_tsquery("hello cloud bucket", FullTextMatchMode.ANY)
    assert built is not None
    fragment, binds = built
    assert fragment == (
        "plainto_tsquery('simple', :t0) || plainto_tsquery('simple', :t1) || plainto_tsquery('simple', :t2)"
    )
    assert binds == {"t0": "hello", "t1": "cloud", "t2": "bucket"}
    _compile(fragment, binds)  # must not raise


def test_any_mode_single_term_reduces_to_one_call() -> None:
    """A single-term ANY query reduces to exactly one plainto_tsquery call — like ALL."""
    built = _build_fulltext_tsquery("hello", FullTextMatchMode.ANY)
    assert built is not None
    fragment, binds = built
    assert fragment == "plainto_tsquery('simple', :t0)"
    assert "||" not in fragment
    assert binds == {"t0": "hello"}


def test_empty_query_returns_none() -> None:
    """Empty or whitespace-only queries have no terms and yield None (caller short-circuits)."""
    assert _build_fulltext_tsquery("", FullTextMatchMode.ANY) is None
    assert _build_fulltext_tsquery("   ", FullTextMatchMode.ANY) is None
    assert _build_fulltext_tsquery("", FullTextMatchMode.ALL) is None
    assert _build_fulltext_tsquery("\t\n ", FullTextMatchMode.ALL) is None


def test_terms_are_bound_params_not_interpolated() -> None:
    """User terms never appear in the SQL fragment text — only :tN placeholders do."""
    built = _build_fulltext_tsquery("DROP TABLE", FullTextMatchMode.ANY)
    assert built is not None
    fragment, binds = built
    # The literal user terms must not be interpolated into the SQL text.
    assert "DROP" not in fragment
    assert "TABLE" not in fragment
    assert binds == {"t0": "DROP", "t1": "TABLE"}
    _compile(fragment, binds)  # compiles despite SQL-keyword-looking terms
