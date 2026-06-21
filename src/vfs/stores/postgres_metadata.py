"""PostgreSQL-backed metadata store, expressed on the shared SQLAlchemy Core schema.

Importable only when the ``postgres`` extra (``asyncpg``) is installed; the URI resolver
guards the import and raises an actionable error otherwise.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import logging
import re
from typing import TYPE_CHECKING, Any, Callable

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from vfs.models import FullTextMatchMode, SearchArtifact, SearchResult, SearchType
from vfs.protocols.search import SearchResponse
from vfs.stores.sql_metadata import BaseSqlMetadataStore

if TYPE_CHECKING:
    from vfs.protocols.search import SearchRequest

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# PostgreSQL NativeTextSearch constants
# ---------------------------------------------------------------------------

#: Stable provider key stored in ``search_meta`` manifests and ``search_text_artifacts``.
_PG_PROVIDER_KEY: str = "vfs.postgres_fts"

#: Short hex digest of the tokenizer/index configuration — bump when config changes.
_PG_PARAMS_HASH: str = hashlib.sha256(b"postgres:tsvector+trgm:english:v1").hexdigest()[:16]


def _build_fulltext_tsquery(query: str, mode: FullTextMatchMode) -> tuple[str, dict[str, str]] | None:
    """Build the tsquery SQL fragment and its bound params for a fulltext query.

    Returns ``(fragment, bindparams)`` where ``fragment`` is a SQL expression containing
    only ``plainto_tsquery('english', :tN)`` calls referencing named placeholders, and
    ``bindparams`` maps each placeholder to the user term it binds.  Returns ``None`` when
    the query has no terms (empty or whitespace-only), so callers can short-circuit to an
    empty response rather than emit invalid SQL.

    - ``ALL`` (default): a single ``plainto_tsquery('english', :query)`` over the whole
      query string — every term must appear (``plainto_tsquery`` AND-combines its lexemes).
    - ``ANY``: the query is split on whitespace and each term gets its own
      ``plainto_tsquery('english', :tN)`` call, OR-combined with the tsquery ``||``
      operator.  A single-term query reduces to exactly one ``plainto_tsquery`` call,
      identical to ``ALL``.

    User terms are carried only as **separate bound parameters** (``:t0``, ``:t1``, …),
    never string-interpolated into SQL.  ``plainto_tsquery`` (unlike ``to_tsquery``) never
    raises on malformed input — it normalizes input to a safe lexeme sequence — so no term
    can produce a syntax error.
    """
    terms = query.split()
    if not terms:
        return None
    if mode == FullTextMatchMode.ANY:
        fragment = " || ".join(f"plainto_tsquery('english', :t{i})" for i in range(len(terms)))
        return fragment, {f"t{i}": term for i, term in enumerate(terms)}
    return "plainto_tsquery('english', :query)", {"query": query}


class _PostgresNativeTextSearch:
    """NativeTextSearch capability backed by PostgreSQL ``pg_trgm`` (regex) and ``to_tsvector`` / ``ts_rank`` (fulltext).

    CONFIDENTIALITY: ``search_text_artifacts`` stores decoded file content (raw text),
    making the metadata database content-bearing at the same sensitivity tier as the blob
    store.  Protect accordingly: encryption at rest, least-privilege DB roles, restricted
    backups/replicas/analytics access.  Text artifacts are content-addressed (shared across
    namespaces at rest); namespace isolation is enforced at the query boundary only.
    GC deletes text artifacts when their content hash is orphaned (no remaining version
    references) or when a params_hash profile is retired.

    Regex path: ``WHERE raw_text ~ :pattern`` evaluated in-engine; the ``gin_trgm_ops``
    GIN index on ``raw_text`` (created in the migration) lets Postgres prune candidate rows
    using extracted trigrams.  Trigram-unfriendly patterns (e.g. ``[0-9]+``) fall back to
    a sequential scan of ``search_text_artifacts`` — correctness and zero-blob-read
    invariant hold; the sub-millisecond latency claim does not.

    Fulltext path: ``plainto_tsquery`` matched against ``to_tsvector('english', raw_text)``,
    ranked by ``ts_rank``.  In ``ALL`` mode a single ``plainto_tsquery('english', :query)``
    is used; in ``ANY`` mode per-term ``plainto_tsquery`` calls are OR-combined with ``||``.
    """

    provider_key: str = _PG_PROVIDER_KEY
    params_hash: str = _PG_PARAMS_HASH

    def __init__(self, store: "PostgresMetadataStore") -> None:
        self._store = store

    async def index_text(
        self,
        version_id: str,
        content_hash: str,
        params_hash: str,
        text: str,
    ) -> SearchArtifact:
        """Upsert a content-addressed text record inside the caller's version transaction.

        Returns a ``ready`` ``external`` ``SearchArtifact`` referencing the text record.
        Infrastructure failures propagate as exceptions and abort the enclosing transaction.
        """
        now = datetime.now(timezone.utc).isoformat()
        async with self._store._operation():
            await self._store._db.execute(
                sa.text(
                    """
                    INSERT INTO search_text_artifacts
                        (provider_key, params_hash, content_hash, raw_text, status, created_at)
                    VALUES (:pk, :ph, :ch, :rt, 'ready', :ca)
                    ON CONFLICT (provider_key, params_hash, content_hash)
                    DO UPDATE SET raw_text   = EXCLUDED.raw_text,
                                  status     = EXCLUDED.status,
                                  created_at = EXCLUDED.created_at
                    """
                ).bindparams(pk=self.provider_key, ph=params_hash, ch=content_hash, rt=text, ca=now)
            )

        artifact_ref = f"{self.provider_key}:{params_hash}:{content_hash}"
        return SearchArtifact(
            status="ready",
            schema_version=1,
            provider_key=self.provider_key,
            provider_version="1",
            params_hash=params_hash,
            content_hash=content_hash,
            created_at=datetime.now(timezone.utc),
            storage="external",
            artifact_ref=artifact_ref,
        )

    async def search_text(
        self,
        request: "SearchRequest",
        visible_version_ids: list[str],
    ) -> SearchResponse:
        """Match content and expand results to visible occurrences.

        Regex: ``text ~ :pattern`` evaluated in-engine (trigram-pruned when extractable
        literals exist); no blob reads for fresh records.
        Fulltext: ``plainto_tsquery`` match ranked by ``ts_rank``.
        """
        from vfs.protocols.search import SearchMetaEntry  # avoid circular at module level

        ch_to_entries: dict[str, list[SearchMetaEntry]] = {}
        for e in request.search_metas:
            ch_to_entries.setdefault(e.content_hash, []).append(e)

        if not ch_to_entries:
            return SearchResponse()

        if request.search_type == SearchType.FULLTEXT:
            return await self._fulltext_search(request.query, ch_to_entries, request.match_mode)
        else:
            return await self._regex_search(request.query, ch_to_entries)

    async def _regex_search(self, pattern: str, ch_to_entries: dict[str, list[Any]]) -> SearchResponse:
        """In-engine regex via ``text ~ :pattern``; GIN index prunes trigram candidates.

        raw_text is fetched for matched rows so per-occurrence SearchResults can be emitted
        — one per matching line — with line_number and match_context populated.  The in-engine
        ``~`` operator still prunes/filters; line extraction is done in-process from the stored
        text (preserves the zero-blob-read invariant).  Semantics mirror
        DefaultSearchProvider._regex_search exactly (GrepMatchesContent spec contract).
        """
        try:
            compiled = re.compile(pattern)
        except re.error:
            return SearchResponse()

        visible_hashes = list(ch_to_entries.keys())
        results: list[SearchResult] = []

        async with self._store._operation():
            rows = (
                await self._store._db.execute(
                    sa.text(
                        """
                        SELECT content_hash, raw_text
                        FROM search_text_artifacts
                        WHERE provider_key = :pk
                          AND params_hash  = :ph
                          AND content_hash = ANY(:hashes)
                          AND raw_text ~ :pattern
                        """
                    ).bindparams(
                        pk=self.provider_key,
                        ph=self.params_hash,
                        hashes=visible_hashes,
                        pattern=pattern,
                    )
                )
            ).fetchall()

        for row in rows:
            ch, raw_text = row[0], row[1]
            if ch in ch_to_entries:
                for line_num, line in enumerate(raw_text.splitlines(), start=1):
                    if compiled.search(line):
                        for entry in ch_to_entries[ch]:
                            results.append(
                                SearchResult(path=entry.path, line_number=line_num, match_context=line.strip())
                            )

        return SearchResponse(results=results)

    async def _fulltext_search(
        self,
        query: str,
        ch_to_entries: dict[str, list[Any]],
        mode: FullTextMatchMode = FullTextMatchMode.ALL,
    ) -> SearchResponse:
        """``ts_rank`` ranked fulltext search via ``plainto_tsquery``.

        ``mode`` selects how the query terms are combined:

        - ``ALL`` (default, strict-AND): a single ``plainto_tsquery('english', :query)`` is
          used in both the ``@@`` match predicate and the ``ts_rank`` score — a document
          must contain every term.
        - ``ANY`` (ranked-OR): the query is split on whitespace and each term gets its own
          ``plainto_tsquery('english', :tN)`` call, OR-combined with the tsquery ``||``
          operator (``plainto_tsquery(:t0) || plainto_tsquery(:t1) || …``).  The same
          OR-combined tsquery is used in both the ``@@`` predicate and the ``ts_rank``
          score, so ``ts_rank`` scores documents matching more or rarer terms higher,
          satisfying the ranked-OR contract.  A single-term query reduces to exactly one
          ``plainto_tsquery`` call — identical to ``ALL``.

        User terms are passed only as **separate bound parameters** (``:t0``, ``:t1``, …),
        never string-interpolated into SQL.  ``plainto_tsquery`` (unlike ``to_tsquery``)
        never raises on malformed input — it normalizes input to a safe lexeme sequence — so
        no term can produce a syntax error.

        An empty or whitespace-only query has no terms and returns an empty
        :class:`SearchResponse` (matching the SQLite backend), rather than emitting an
        invalid empty ``||`` expression.
        """
        built = _build_fulltext_tsquery(query, mode)
        if built is None:
            return SearchResponse()
        tsquery_fragment, term_binds = built

        visible_hashes = list(ch_to_entries.keys())
        results: list[SearchResult] = []

        async with self._store._operation():
            rows = (
                await self._store._db.execute(
                    sa.text(
                        # tsquery_fragment contains only literal plainto_tsquery() calls
                        # referencing :tN / :query placeholders — never user text.
                        f"""
                        SELECT content_hash,
                               ts_rank(to_tsvector('english', raw_text),
                                       {tsquery_fragment}) AS score
                        FROM search_text_artifacts
                        WHERE provider_key = :pk
                          AND params_hash  = :ph
                          AND content_hash = ANY(:hashes)
                          AND to_tsvector('english', raw_text)
                              @@ ({tsquery_fragment})
                        ORDER BY score DESC
                        """  # noqa: S608 — fragment is module-built placeholders only; terms are bound params
                    ).bindparams(
                        pk=self.provider_key,
                        ph=self.params_hash,
                        hashes=visible_hashes,
                        **term_binds,
                    )
                )
            ).fetchall()

        for row in rows:
            ch, score = row[0], float(row[1]) if row[1] is not None else 0.0
            if ch in ch_to_entries:
                for entry in ch_to_entries[ch]:
                    results.append(SearchResult(path=entry.path, score=score))

        return SearchResponse(results=results)

    async def delete_text_artifacts(
        self,
        content_hashes: list[str],
        retired_params_hashes: list[str],
    ) -> None:
        """Delete text artifacts for orphaned content hashes or retired params profiles."""
        async with self._store._operation():
            if content_hashes:
                await self._store._db.execute(
                    sa.text(
                        "DELETE FROM search_text_artifacts WHERE provider_key = :pk AND content_hash = ANY(:hashes)"
                    ).bindparams(pk=self.provider_key, hashes=content_hashes)
                )
            if retired_params_hashes:
                await self._store._db.execute(
                    sa.text(
                        "DELETE FROM search_text_artifacts WHERE provider_key = :pk AND params_hash = ANY(:hashes)"
                    ).bindparams(pk=self.provider_key, hashes=retired_params_hashes)
                )


# ---------------------------------------------------------------------------
# PostgreSQL metadata store
# ---------------------------------------------------------------------------


class PostgresMetadataStore(BaseSqlMetadataStore):
    """MetadataStore implementation backed by PostgreSQL via SQLAlchemy Core + asyncpg.

    A thin dialect adapter over :class:`BaseSqlMetadataStore`: it supplies the asyncpg
    engine and the PostgreSQL ``insert`` construct for ``ON CONFLICT`` upserts. All
    file/version/permission/audit CRUD, the ``WHERE current_version_number = ?``
    compare-and-swap, and the ``BEGIN``/``COMMIT``/``ROLLBACK`` ``transaction()`` are
    inherited unchanged from the shared base, so SQLite and PostgreSQL share one schema
    and one concurrency-control path. The shared schema renders the ``search_meta`` and
    ``detail`` columns as native ``JSONB`` on PostgreSQL, so those fields round-trip as
    structured JSON automatically.

    Connection model: like the SQLite adapter, a single long-lived
    :class:`~sqlalchemy.ext.asyncio.AsyncConnection` is held for the store's lifetime.
    Every operation runs under the base store's ``asyncio.Lock`` and commits (or rolls
    back) at its own boundary unless wrapped in :meth:`transaction`. This keeps the CAS and
    transaction semantics identical across both SQL backends. The lock serializes
    operations on one store instance; connection pooling for concurrency is a deliberate
    future optimization, not built now.

    Schema creation uses ``metadata.create_all`` (inherited :meth:`initialize`) so the
    store is self-contained for tests; production schema evolution is owned by the Alembic
    migrations.

    :meth:`native_text_search` exposes the :class:`_PostgresNativeTextSearch` capability
    (``pg_trgm`` GIN + ``to_tsvector`` fulltext) when asyncpg is present (always, since
    this class is only importable when ``asyncpg`` is installed).
    """

    def __init__(self, uri: str) -> None:
        """Store the connection URI, translating it to the asyncpg driver for SQLAlchemy.

        The resolver passes the full URI (e.g. ``postgresql://user:pass@host:5432/db``);
        SQLAlchemy needs the ``postgresql+asyncpg`` driver name. No connection is opened
        here — that happens in :meth:`initialize`.

        TLS note: SQLAlchemy's asyncpg dialect expects ``ssl=`` (not libpq's ``sslmode=``)
        in the URL query string, so a ``?sslmode=...`` copied from a psycopg DSN will not
        take effect here. Connection tuning (TLS, pooling) is deferred by design.
        """
        super().__init__()
        self._url = make_url(uri).set(drivername="postgresql+asyncpg")
        self._nts = _PostgresNativeTextSearch(self)

    def _create_engine(self) -> AsyncEngine:
        return create_async_engine(self._url)

    @property
    def _dialect_insert(self) -> Callable[..., Any]:
        return postgresql_insert

    async def initialize(self) -> None:
        """Initialize the base schema, then attempt to set up the pg_trgm GIN index.

        After ``metadata.create_all`` the store tries to create the ``pg_trgm`` extension
        and a GIN index on ``search_text_artifacts.raw_text`` (``gin_trgm_ops``).  Both
        operations succeed when the connected role has ``CREATE EXTENSION`` and ``CREATE
        INDEX`` privileges; if they fail (e.g. the role is unprivileged), a warning is
        logged and the store remains functional — regex searches fall back to a sequential
        scan of ``search_text_artifacts`` rather than the accelerated trigram path.

        Note on schema drift: the Alembic migration 0002 creates the same extension and
        GIN index; production deployments that apply migrations first will already have
        them.  ``initialize()`` uses ``CREATE EXTENSION IF NOT EXISTS`` and
        ``CREATE INDEX IF NOT EXISTS`` so duplicate creation is a no-op.

        Superuser requirement: ``CREATE EXTENSION pg_trgm`` requires superuser or
        ``pg_extension_owner_transfer`` privilege in standard PostgreSQL.  If the connected
        role is unprivileged, grant the privilege out-of-band or accept the sequential-scan
        fallback.  See also: migration 0002 docstring.
        """
        await super().initialize()
        await self._setup_trgm()

    async def _setup_trgm(self) -> None:
        """Create the pg_trgm extension and GIN index; log and skip on privilege errors."""
        async with self._lock:
            try:
                await self._conn.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS pg_trgm")
                await self._conn.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS idx_sta_trgm ON search_text_artifacts USING gin(raw_text gin_trgm_ops)"
                )
                await self._conn.commit()
            except Exception as exc:  # noqa: BLE001 — best-effort, privilege error is common
                try:
                    await self._conn.rollback()
                except Exception as rollback_exc:
                    _log.debug("Rollback after pg_trgm setup failure also failed: %s", rollback_exc)
                _log.warning(
                    "pg_trgm extension or GIN index creation failed (insufficient privilege?); "
                    "regex searches will use a sequential scan. "
                    "Grant CREATE EXTENSION privilege or run migration 0002 as a superuser. "
                    "Error: %s",
                    exc,
                )

    def native_text_search(self) -> _PostgresNativeTextSearch:
        """Return the PostgreSQL NativeTextSearch capability (always available)."""
        return self._nts
