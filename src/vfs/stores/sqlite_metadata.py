"""SQLite-backed metadata store, expressed on the shared SQLAlchemy Core schema."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import logging
import re
from typing import TYPE_CHECKING, Any, Callable

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

from vfs.models import FullTextMatchMode, SearchArtifact, SearchResult, SearchType
from vfs.protocols.search import SearchResponse
from vfs.stores.sql_metadata import BaseSqlMetadataStore

if TYPE_CHECKING:
    from vfs.protocols.search import SearchRequest

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SQLite FTS5 NativeTextSearch constants
# ---------------------------------------------------------------------------

#: Stable provider key stored in ``search_meta`` manifests and ``search_text_artifacts``.
_FTS5_PROVIDER_KEY: str = "vfs.sqlite_fts5"

#: Short hex digest of the tokenizer/index configuration — bump when config changes.
_FTS5_PARAMS_HASH: str = hashlib.sha256(b"sqlite:fts5:tokenize=trigram:v1").hexdigest()[:16]


def _fts5_literal_from_pattern(pattern: str) -> str | None:
    """Extract the longest mandatory literal substring (≥ 3 chars) from a regex pattern.

    Used as a FTS5 trigram prefilter: a literal of ≥ 3 chars can be handed to
    ``search_fts MATCH '"literal"'`` to prune candidate rows before the full
    in-process ``re.search`` verification.  Returns ``None`` (fall back to full scan)
    when:

    - The pattern contains alternation ``|`` outside a character class — the literal
      from one branch is not mandatory for every possible match, causing false negatives
      (e.g. ``foo|barbaz``: documents matching ``foo`` would be pruned away).
    - The pattern contains ``?`` or ``*`` outside a character class — an optional or
      zero-or-more quantifier means no extracted literal is guaranteed to be present in
      every matching document.
    - No literal run of ≥ 3 chars exists (e.g. ``[0-9]+``).

    The conservative invariant: prune only when the extracted literal is mandatory for
    EVERY possible match of the pattern.  Patterns that are a plain concatenation of
    literals and always-≥1 constructs (``+``, ``{n}``, ``{n,m}`` with n≥1) with no
    ``|`` outside a character class are safe to prune.

    Character classes (``[...]``) are skipped in their entirety so characters inside
    the class are never mistaken for literals (e.g. ``0-9`` inside ``[0-9]+`` must not
    produce the spurious literal "0-9").
    """
    best: list[str] = []
    current: list[str] = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\" and i + 1 < len(pattern):
            nxt = pattern[i + 1]
            if nxt in r"\.^$*+?{}[]|()":
                # escaped metachar → treat as literal char
                current.append(nxt)
            else:
                # shorthand like \d, \w — not a bare literal
                if len(current) > len(best):
                    best = current[:]
                current = []
            i += 2
        elif ch == "[":
            # Character class: skip the entire [...] block — its contents are not literals.
            if len(current) > len(best):
                best = current[:]
            current = []
            i += 1
            # A ']' immediately after '[' or '[^' is a literal (part of the class), not
            # the closing bracket — consume it so the scan loop doesn't stop early.
            if i < len(pattern) and pattern[i] == "^":
                i += 1
            if i < len(pattern) and pattern[i] == "]":
                i += 1
            while i < len(pattern) and pattern[i] != "]":
                i += 1
            if i < len(pattern):
                i += 1  # consume the closing ']'
        elif ch in "|?*":
            # Alternation or optional quantifier: cannot guarantee any extracted literal
            # is mandatory for every possible match — fall back to full scan.
            return None
        elif ch in r"\.^$+{}()":
            if len(current) > len(best):
                best = current[:]
            current = []
            i += 1
        else:
            current.append(ch)
            i += 1
    if len(current) > len(best):
        best = current
    seq = "".join(best)
    return seq if len(seq) >= 3 else None


class _SQLiteNativeTextSearch:
    """NativeTextSearch capability backed by SQLite FTS5 with the trigram tokenizer.

    CONFIDENTIALITY: ``search_text_artifacts`` stores decoded file content (raw text),
    making the metadata database content-bearing at the same sensitivity tier as the blob
    store.  Protect accordingly: encryption at rest, least-privilege DB roles, restricted
    backups/replicas/analytics access.  Text artifacts are content-addressed (shared across
    namespaces at rest); namespace isolation is enforced at the query boundary only.
    GC deletes text artifacts when their content hash is orphaned (no remaining version
    references) or when a params_hash profile is retired.

    All methods re-use the store's ``_operation()`` context so they compose correctly
    inside a ``transaction()`` block (the ``_in_txn`` ContextVar makes ``_operation()``
    a no-op when a transaction is already held, avoiding re-lock / re-commit).
    """

    provider_key: str = _FTS5_PROVIDER_KEY
    params_hash: str = _FTS5_PARAMS_HASH

    def __init__(self, store: "SQLiteMetadataStore") -> None:
        self._store = store

    # --- NativeTextSearch protocol ---

    async def index_text(
        self,
        version_id: str,
        content_hash: str,
        params_hash: str,
        text: str,
    ) -> SearchArtifact:
        """Upsert a content-addressed text record inside the caller's version transaction.

        Called from within ``VFS.write``'s ``transaction()`` block so that a rollback of
        the version write also rolls back the text artifact.  The upsert is idempotent:
        re-indexing the same ``(provider_key, params_hash, content_hash)`` with identical
        text is a no-op at the DB level.

        Returns a ``ready`` ``external`` ``SearchArtifact`` referencing the text record.
        Infrastructure failures propagate as exceptions and abort the enclosing transaction.
        """
        now = datetime.now(timezone.utc).isoformat()
        async with self._store._operation():
            # Upsert into the canonical text store (raw_text is the verification substrate).
            await self._store._db.exec_driver_sql(
                """
                INSERT INTO search_text_artifacts
                    (provider_key, params_hash, content_hash, raw_text, status, created_at)
                VALUES (?, ?, ?, ?, 'ready', ?)
                ON CONFLICT(provider_key, params_hash, content_hash)
                DO UPDATE SET raw_text = excluded.raw_text,
                              status   = excluded.status,
                              created_at = excluded.created_at
                """,
                (self.provider_key, params_hash, content_hash, text, now),
            )
            # Sync the FTS5 derived index: delete any stale entry then insert fresh.
            await self._store._db.exec_driver_sql(
                "DELETE FROM search_fts WHERE provider_key=? AND params_hash=? AND content_hash=?",
                (self.provider_key, params_hash, content_hash),
            )
            await self._store._db.exec_driver_sql(
                "INSERT INTO search_fts(provider_key, params_hash, content_hash, raw_text) VALUES(?,?,?,?)",
                (self.provider_key, params_hash, content_hash, text),
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
        """Match content against stored raw text and expand to visible occurrences.

        No blob reads are issued for fresh (ready) records — all verification is
        performed against the text stored in ``search_text_artifacts`` (for regex) or the
        FTS5 autonomous index (for fulltext).

        Regex path:
          - If the pattern contains a literal sequence of ≥ 3 chars, the FTS5 trigram
            index is used to prune candidates.  Trigram-unfriendly patterns (e.g.
            ``[0-9]+``) skip the FTS5 prune and scan ``search_text_artifacts`` directly.
          - Pruned candidates are verified in-process with ``re.search``.

        Fulltext path:
          - FTS5 BM25 ranking via the ``rank`` auxiliary column (lower = more relevant).
          - Results ordered by relevance; score = ``abs(rank)`` (≥ 0, higher = better).
        """
        # Build content_hash → [SearchMetaEntry] map from the visible entries.
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
        """In-process regex verification against DB-resident text (zero blob reads).

        For each matched document, per-occurrence SearchResults are emitted — one per
        matching line — with line_number and match_context populated.  Semantics mirror
        DefaultSearchProvider._regex_search exactly (GrepMatchesContent spec contract).
        """
        try:
            compiled = re.compile(pattern)
        except re.error:
            return SearchResponse()

        literal = _fts5_literal_from_pattern(pattern)
        results: list[SearchResult] = []

        async with self._store._operation():
            if literal and '"' not in literal:
                # FTS5 trigram prune: phrase-query the literal, then verify with re.
                fts_query = f'"{literal}"'
                rows = await self._store._execute_fetchall(
                    "SELECT content_hash, raw_text FROM search_fts "
                    "WHERE raw_text MATCH ? AND provider_key=? AND params_hash=?",
                    (fts_query, self.provider_key, self.params_hash),
                )
                # Filter to visible hashes, re-verify (FTS5 prune may over-select),
                # then emit one result per matching line with line_number + match_context.
                for row in rows:
                    ch, raw_text = row[0], row[1]
                    if ch not in ch_to_entries:
                        continue
                    for line_num, line in enumerate(raw_text.splitlines(), start=1):
                        if compiled.search(line):
                            for entry in ch_to_entries[ch]:
                                results.append(
                                    SearchResult(path=entry.path, line_number=line_num, match_context=line.strip())
                                )
            else:
                # No extractable literal (or literal contains '"') — scan the main table.
                visible_hashes = list(ch_to_entries.keys())
                placeholders = ",".join("?" * len(visible_hashes))
                rows = await self._store._execute_fetchall(
                    f"SELECT content_hash, raw_text FROM search_text_artifacts "  # noqa: S608 — {placeholders} is "?,?,?" only; table/column names are module constants
                    f"WHERE provider_key=? AND params_hash=? AND content_hash IN ({placeholders})",
                    (self.provider_key, self.params_hash) + tuple(visible_hashes),
                )
                for row in rows:
                    ch, raw_text = row[0], row[1]
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
        """FTS5 BM25 ranked fulltext search — results ordered by relevance.

        The user query is tokenized on whitespace and each token is quoted as an FTS5
        phrase (double-quoted, with internal double-quotes doubled).  Tokens are treated as
        literal words rather than FTS5 operators, so ``c++`` does not raise a syntax error
        and a token like ``OR`` does not become boolean OR.

        ``mode`` selects how the token phrases are combined:

        - ``ALL`` (default, strict-AND): phrases are space-joined (``"tok1" "tok2"``),
          matching ``plainto_tsquery`` semantics — a document must contain every term.
        - ``ANY`` (ranked-OR): phrases are OR-joined (``"tok1" OR "tok2"``) — a document
          matching at least one term is returned.

        Injection safety is identical across modes: each token is double-quoted with the
        same internal ``"`` → ``""`` escaping, so user input is always a literal phrase.
        The only difference is the join string — a bare ASCII ``" "`` (ALL) or ``" OR "``
        (ANY) inserted by this method, never derived from user input.  BM25 ranking
        (``ORDER BY rank``) already scores documents matching more and rarer terms higher,
        satisfying the ranked-OR contract without extra scoring machinery.
        """
        tokens = query.split()
        if not tokens:
            return SearchResponse()
        # Quote each token as an FTS5 phrase: "token" — internal '"' → '""'
        phrases = ['"' + tok.replace('"', '""') + '"' for tok in tokens]
        # Join with a bare ASCII operator literal (not user-derived): " OR " for ANY,
        # " " (implicit AND) for ALL.
        join = " OR " if mode == FullTextMatchMode.ANY else " "
        fts_query = join.join(phrases)

        results: list[SearchResult] = []
        async with self._store._operation():
            rows = await self._store._execute_fetchall(
                "SELECT content_hash, rank FROM search_fts "
                "WHERE raw_text MATCH ? AND provider_key=? AND params_hash=? "
                "ORDER BY rank",
                (fts_query, self.provider_key, self.params_hash),
            )
            for row in rows:
                ch = row[0]
                score = abs(float(row[1])) if row[1] is not None else 0.0
                if ch not in ch_to_entries:
                    continue
                for entry in ch_to_entries[ch]:
                    results.append(SearchResult(path=entry.path, score=score))
        return SearchResponse(results=results)

    async def delete_text_artifacts(
        self,
        content_hashes: list[str],
        retired_params_hashes: list[str],
    ) -> None:
        """Delete text artifacts for orphaned content hashes or retired params profiles.

        Called by the GC sweep.  Both ``search_text_artifacts`` (the raw-text store) and
        ``search_fts`` (the derived FTS5 index) are pruned to keep them consistent.
        """
        async with self._store._operation():
            if content_hashes:
                ph = ",".join("?" * len(content_hashes))
                await self._store._db.exec_driver_sql(
                    f"DELETE FROM search_text_artifacts WHERE provider_key=? AND content_hash IN ({ph})",  # noqa: S608 — {ph} is "?,?,?" only; table/column names are module constants
                    (self.provider_key,) + tuple(content_hashes),
                )
                await self._store._db.exec_driver_sql(
                    f"DELETE FROM search_fts WHERE provider_key=? AND content_hash IN ({ph})",  # noqa: S608
                    (self.provider_key,) + tuple(content_hashes),
                )
            if retired_params_hashes:
                ph = ",".join("?" * len(retired_params_hashes))
                await self._store._db.exec_driver_sql(
                    f"DELETE FROM search_text_artifacts WHERE provider_key=? AND params_hash IN ({ph})",  # noqa: S608
                    (self.provider_key,) + tuple(retired_params_hashes),
                )
                await self._store._db.exec_driver_sql(
                    f"DELETE FROM search_fts WHERE provider_key=? AND params_hash IN ({ph})",  # noqa: S608
                    (self.provider_key,) + tuple(retired_params_hashes),
                )


# ---------------------------------------------------------------------------
# SQLite metadata store
# ---------------------------------------------------------------------------


class SQLiteMetadataStore(BaseSqlMetadataStore):
    """MetadataStore implementation backed by SQLite via SQLAlchemy Core + aiosqlite.

    A thin dialect adapter over :class:`BaseSqlMetadataStore`: it supplies the aiosqlite
    engine (including the ``:memory:`` ``StaticPool`` handling), the SQLite ``insert``
    construct for ``ON CONFLICT`` upserts, and a post-connect step enabling WAL mode.
    All file/version/permission/audit CRUD and CAS live in the shared base.

    After base initialization this store also attempts to create an FTS5 virtual table
    ``search_fts`` (``tokenize='trigram'``, requires SQLite ≥ 3.34).  When the trigram
    tokenizer is available, :meth:`native_text_search` returns a live
    :class:`_SQLiteNativeTextSearch` capability; otherwise it returns ``None`` and regex
    falls back to brute-force via the guarded reader.
    """

    def __init__(self, db_path: str) -> None:
        super().__init__()
        self._db_path = db_path
        self._nts: _SQLiteNativeTextSearch | None = None

    def _create_engine(self) -> AsyncEngine:
        if self._db_path == ":memory:":
            # A single shared connection keeps the in-memory database alive for the store's lifetime.
            return create_async_engine(
                "sqlite+aiosqlite:///:memory:",
                poolclass=StaticPool,
                connect_args={"check_same_thread": False},
            )
        return create_async_engine(f"sqlite+aiosqlite:///{self._db_path}")

    @property
    def _dialect_insert(self) -> Callable[..., Any]:
        return sqlite_insert

    async def _post_connect(self, conn: AsyncConnection) -> None:
        """Enable write-ahead logging for better concurrency on file-backed databases."""
        await conn.exec_driver_sql("PRAGMA journal_mode=WAL")

    async def initialize(self) -> None:
        """Initialize the base schema, then attempt to set up the FTS5 derived index.

        The FTS5 virtual table ``search_fts`` requires SQLite ≥ 3.34 for the ``trigram``
        tokenizer.  If creation fails (older SQLite build), a warning is logged and
        :meth:`native_text_search` returns ``None`` — regex searches fall back to
        brute-force via the guarded reader; fulltext remains unsupported.
        """
        await super().initialize()
        await self._setup_fts5()

    async def _setup_fts5(self) -> None:
        """Create the FTS5 autonomous table; set ``_nts`` on success, log and skip on failure."""
        async with self._lock:
            try:
                await self._conn.exec_driver_sql(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5("
                    "provider_key UNINDEXED, "
                    "params_hash UNINDEXED, "
                    "content_hash UNINDEXED, "
                    "raw_text, "
                    "tokenize='trigram'"
                    ")"
                )
                await self._conn.commit()
                self._nts = _SQLiteNativeTextSearch(self)
            except Exception as exc:  # noqa: BLE001 — graceful degradation, log + continue
                try:
                    await self._conn.rollback()
                except Exception as rollback_exc:
                    _log.debug("Rollback after FTS5 setup failure also failed: %s", rollback_exc)
                _log.warning(
                    "FTS5 trigram tokenizer unavailable (requires SQLite ≥ 3.34.0); "
                    "native_text_search() returns None — regex falls back to brute-force. "
                    "Error: %s",
                    exc,
                )

    def native_text_search(self) -> _SQLiteNativeTextSearch | None:
        """Return the FTS5-backed NativeTextSearch capability, or None if unavailable.

        ``None`` is returned when the SQLite build does not support the trigram tokenizer
        (SQLite < 3.34.0).  In that case the VFS falls back to brute-force regex via the
        guarded reader, and fulltext search is unsupported.
        """
        return self._nts
