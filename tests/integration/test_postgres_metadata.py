"""Integration tests for PostgresMetadataStore against a real PostgreSQL server.

These tests require a reachable PostgreSQL server. Provide its DSN via the
``AIVFS_TEST_POSTGRES_DSN`` environment variable, e.g.::

    AIVFS_TEST_POSTGRES_DSN=postgresql://aivfs:aivfs@localhost:5432/aivfs

Start a local server with the Docker Compose fixture::

    docker compose -f tests/integration/docker-compose.yml up -d postgres

The whole module is skipped when ``asyncpg`` is not installed, the DSN is unset, or the
server is unreachable, so the default test run stays green without a database.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import importlib.util
import os
import re

import pytest
import pytest_asyncio
from ulid import ULID

from vfs.errors import ConflictError
from vfs.models import AuditEvent, FileMeta, FullTextMatchMode, Permission, SearchType, VersionMeta

_DSN = os.environ.get("AIVFS_TEST_POSTGRES_DSN")

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("asyncpg") is None or not _DSN,
    reason="requires asyncpg and AIVFS_TEST_POSTGRES_DSN pointing at a reachable PostgreSQL server",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest_asyncio.fixture(scope="session")
async def worker_dsn():
    """Provision a dedicated PostgreSQL database for this xdist worker, yield a DSN for it.

    Finding 3: the previous fixture dropped+recreated ALL tables in whatever
    ``AIVFS_TEST_POSTGRES_DSN`` pointed at, so parallel xdist workers clobbered each other
    and a misconfigured DSN could destroy non-test tables. Instead, each worker owns a database
    named ``aivfs_test_<worker>``. We connect to a *maintenance* database (the DSN with its
    database swapped to ``postgres``) with an AUTOCOMMIT engine, DROP/CREATE the worker's
    database there, and never touch the tables of the DSN's own database.

    The configured role must have ``CREATEDB`` (the default superuser does). Skips cleanly
    if the maintenance connection fails.
    """
    from sqlalchemy import text
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import SQLAlchemyError
    from sqlalchemy.ext.asyncio import create_async_engine

    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    test_db = f"aivfs_test_{worker}"

    base_url = make_url(_DSN).set(drivername="postgresql+asyncpg")
    maint_url = base_url.set(database="postgres")
    maint_engine = create_async_engine(maint_url, isolation_level="AUTOCOMMIT")

    try:
        async with maint_engine.connect() as conn:
            # WITH FORCE terminates any lingering connections to the test DB before dropping.
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{test_db}" WITH (FORCE)'))
            await conn.execute(text(f'CREATE DATABASE "{test_db}"'))
    except (SQLAlchemyError, OSError) as exc:  # unreachable server / auth / missing CREATEDB
        await maint_engine.dispose()
        pytest.skip(f"PostgreSQL maintenance connection failed at AIVFS_TEST_POSTGRES_DSN: {exc}")

    # str(URL) masks the password as "***"; render_as_string(hide_password=False) exposes it
    # for the adapter so asyncpg does not receive a literal three-asterisk password string.
    yield base_url.set(database=test_db).render_as_string(hide_password=False)

    try:
        async with maint_engine.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{test_db}" WITH (FORCE)'))
    except (SQLAlchemyError, OSError):
        pass  # best-effort teardown; the next run recreates it anyway
    finally:
        await maint_engine.dispose()


@pytest_asyncio.fixture
async def pg_store(worker_dsn):
    """A connected PostgresMetadataStore with freshly recreated tables in the worker's DB.

    Dropping and recreating all tables before each test isolates reruns. This is safe
    because each worker owns its own ``aivfs_test_<worker>`` database (see ``worker_dsn``);
    we never touch the tables of the original DSN's database. Skips cleanly if the worker
    database is unreachable.
    """
    from sqlalchemy.exc import SQLAlchemyError

    from vfs.stores.postgres_metadata import PostgresMetadataStore
    from vfs.stores.schema import metadata

    store = PostgresMetadataStore(worker_dsn)
    try:
        store._engine = store._create_engine()
        store._conn = await store._engine.connect()
        await store._conn.run_sync(metadata.drop_all)
        await store._conn.run_sync(metadata.create_all)
        await store._conn.commit()
    except (SQLAlchemyError, OSError) as exc:  # unreachable server / auth failure
        await store.close()
        pytest.skip(f"PostgreSQL worker database unreachable: {exc}")

    yield store
    await store.close()


@pytest_asyncio.fixture
async def sqlite_nts():
    """An in-memory SQLite ``NativeTextSearch`` alongside ``pg_store`` for cross-backend tests.

    Mirrors the construction in ``tests/unit/test_native_text_search.py``: build a
    ``SQLiteMetadataStore(":memory:")``, initialize it, and yield its ``native_text_search()``
    capability.  Skips cleanly when the SQLite build lacks the FTS5 trigram tokenizer.
    """
    from vfs.stores.sqlite_metadata import SQLiteMetadataStore

    store = SQLiteMetadataStore(":memory:")
    await store.initialize()
    nts = store.native_text_search()
    if nts is None:
        await store.close()
        pytest.skip("FTS5 trigram tokenizer not available (SQLite < 3.34)")
    yield nts
    await store.close()


@pytest.mark.asyncio
async def test_round_trip_jsonb(pg_store):
    """PostgresAdapterRoundTrip: file + version with non-empty search_meta, an audit event
    with non-empty detail, and a permission with operations all round-trip via JSONB."""
    now = _now()
    search_meta = {"lang": "python", "tags": ["a", "b"], "nested": {"score": 0.5}}
    version = VersionMeta(
        id=str(ULID()),
        file_path="/src/a.py",
        namespace_id="ns1",
        version_number=1,
        content_hash="hash1",
        size=42,
        created_at=now,
        created_by="principal1",
        search_meta=search_meta,
    )
    await pg_store.put_version(version, expected_version=None)

    file_meta = await pg_store.get_file("ns1", "/src/a.py")
    assert file_meta is not None
    assert file_meta.path == "/src/a.py"
    assert file_meta.current_version_number == 1

    fetched_version = await pg_store.get_version("ns1", "/src/a.py", 1)
    assert fetched_version is not None
    assert fetched_version.search_meta == search_meta

    detail = {"reason": "manual", "fields": [1, 2, 3], "meta": {"k": "v"}}
    event = AuditEvent(
        event_id=str(ULID()),
        timestamp=now,
        namespace_id="ns1",
        principal_id="principal1",
        operation="write",
        path="/src/a.py",
        version_id=version.id,
        detail=detail,
    )
    await pg_store.append_audit_event(event)

    # Read the audit detail back through SQLAlchemy Core for a typed JSONB round-trip.
    from sqlalchemy import select

    from vfs.stores.schema import audit_events

    detail_row = (
        await pg_store._db.execute(select(audit_events.c.detail).where(audit_events.c.event_id == event.event_id))
    ).first()
    assert detail_row is not None
    assert detail_row[0] == detail

    permission = Permission(
        id=str(ULID()),
        principal_id="principal1",
        namespace_id="ns1",
        path_prefix="/src/",
        operations={"read", "write"},
        created_at=now,
    )
    await pg_store.set_permission(permission)
    assert await pg_store.check_permission("principal1", "ns1", "/src/a.py", "read") is True
    assert await pg_store.check_permission("principal1", "ns1", "/src/a.py", "write") is True
    assert await pg_store.check_permission("principal1", "ns1", "/src/a.py", "delete") is False


@pytest.mark.asyncio
async def test_read_leaves_no_open_transaction(pg_store):
    """Finding 2 on Postgres: a read commits inside _operation(), so the held connection is
    not left idle-in-transaction afterward."""
    base = FileMeta(
        namespace_id="ns1",
        path="/a.py",
        current_version_id="v0",
        current_version_number=1,
        created_at=_now(),
        updated_at=_now(),
    )
    await pg_store.put_file(base)
    result = await pg_store.get_file("ns1", "/a.py")
    assert result is not None
    assert pg_store._db.in_transaction() is False


@pytest.mark.asyncio
async def test_cas_conflict_at_write_site(pg_store):
    """CASConflictDetected: a file advanced to version 5 rejects put_version(expected=3),
    leaves the pointer at 5, and inserts no orphan version row."""
    for i in range(1, 6):
        version = VersionMeta(
            id=str(ULID()),
            file_path="/a.py",
            namespace_id="ns1",
            version_number=i,
            content_hash=f"h{i}",
            size=10,
            created_at=_now(),
            created_by="principal1",
        )
        await pg_store.put_version(version, expected_version=None if i == 1 else i - 1)

    stale = VersionMeta(
        id=str(ULID()),
        file_path="/a.py",
        namespace_id="ns1",
        version_number=6,
        content_hash="h6",
        size=10,
        created_at=_now(),
        created_by="principal1",
    )
    with pytest.raises(ConflictError):
        await pg_store.put_version(stale, expected_version=3)

    file_meta = await pg_store.get_file("ns1", "/a.py")
    assert file_meta.current_version_number == 5
    versions = await pg_store.list_versions("ns1", "/a.py", limit=100)
    assert len(versions) == 5
    assert stale.id not in {v.id for v in versions}


@pytest.mark.asyncio
async def test_transaction_rollback_on_error(pg_store):
    """TransactionRollbackOnError: a write inside transaction() that then raises rolls back
    all writes performed within the transaction."""
    base = FileMeta(
        namespace_id="ns1",
        path="/base.py",
        current_version_id="v0",
        current_version_number=1,
        created_at=_now(),
        updated_at=_now(),
    )
    await pg_store.put_file(base)

    with pytest.raises(RuntimeError):
        async with pg_store.transaction():
            await pg_store.put_file(
                FileMeta(
                    namespace_id="ns1",
                    path="/in_txn.py",
                    current_version_id="v1",
                    current_version_number=1,
                    created_at=_now(),
                    updated_at=_now(),
                )
            )
            raise RuntimeError("boom")

    # The pre-transaction write survives; the in-transaction write was rolled back.
    assert await pg_store.get_file("ns1", "/base.py") is not None
    assert await pg_store.get_file("ns1", "/in_txn.py") is None


@pytest.mark.asyncio
async def test_set_name_duplicate_raises_conflict_and_store_stays_usable(pg_store):
    """A different entity claiming an existing display name raises ConflictError and the
    store stays usable afterward against a live asyncpg connection. This exercises the
    real-IntegrityError recovery path: the failed INSERT aborts the asyncpg transaction,
    so set_name must roll back before the store can serve further requests."""
    await pg_store.set_name("namespace", "id_A", "shared")

    with pytest.raises(ConflictError):
        await pg_store.set_name("namespace", "id_B", "shared")

    # After the aborted statement the connection is recovered and usable.
    assert await pg_store.resolve_name("namespace", "shared") == "id_A"
    await pg_store.set_name("namespace", "id_C", "other")
    assert await pg_store.resolve_name("namespace", "other") == "id_C"

    # The legitimate same-entity rename still works.
    await pg_store.set_name("namespace", "id_A", "renamed")
    assert await pg_store.resolve_name("namespace", "renamed") == "id_A"


# ---------------------------------------------------------------------------
# NativeTextSearch (pg_trgm + tsvector) tests
# ---------------------------------------------------------------------------


def _ch(text: str) -> str:
    """Deterministic content-hash for test text (SHA-256 hex)."""
    return hashlib.sha256(text.encode()).hexdigest()


def _search_meta_entry(path: str, content_hash: str):
    """Build a minimal SearchMetaEntry for use in SearchRequest.search_metas."""
    from vfs.protocols.search import SearchMetaEntry

    return SearchMetaEntry(
        version_id=str(ULID()),
        path=path,
        content_hash=content_hash,
        size=len(content_hash),
        updated_at=_now(),
    )


def _req(query: str, search_type: SearchType, entries: list, match_mode: FullTextMatchMode | None = None):
    """Build a SearchRequest; read_content is None (Postgres NTS never calls it).

    ``match_mode`` is omitted from the constructor when ``None`` so its default (``ALL``)
    is exercised; pass an explicit mode to drive the ANY/ALL fulltext paths.
    """
    from vfs.protocols.search import SearchRequest

    kwargs: dict = {}
    if match_mode is not None:
        kwargs["match_mode"] = match_mode
    return SearchRequest(
        query=query,
        scope="/",
        search_type=search_type,
        search_metas=entries,
        read_content=None,  # type: ignore[arg-type]
        **kwargs,
    )


class TestPostgresNativeTextSearch:
    """NativeTextSearch capability integration tests for PostgreSQL.

    Covers regex (pg_trgm), fulltext (tsvector + ts_rank), delete_text_artifacts,
    and the brute-force equivalence contract (including trigram-unfriendly ``[0-9]+``
    and alternation patterns).

    All tests skip cleanly when ``asyncpg`` is missing, ``AIVFS_TEST_POSTGRES_DSN`` is
    unset, or the server is unreachable (inherited from the module-level ``pytestmark``).
    """

    @pytest.mark.asyncio
    async def test_index_produces_ready_external_artifact(self, pg_store):
        """IndexOnWriteProducesExternalArtifact (Postgres): index_text returns a ready external artifact."""
        nts = pg_store.native_text_search()
        ch = _ch("hello postgres world")
        artifact = await nts.index_text(str(ULID()), ch, nts.params_hash, "hello postgres world")

        assert artifact.status == "ready"
        assert artifact.storage == "external"
        assert artifact.provider_key == nts.provider_key
        assert artifact.content_hash == ch
        assert artifact.artifact_ref is not None

    @pytest.mark.asyncio
    async def test_regex_round_trip(self, pg_store):
        """AcceleratedRegexAvoidsBlobReads (Postgres): regex search returns matching paths, no blob reads."""
        nts = pg_store.native_text_search()
        ch_a = _ch("foo bar baz unique")
        ch_b = _ch("hello world test")
        ch_c = _ch("no numbers here at all")

        await nts.index_text(str(ULID()), ch_a, nts.params_hash, "foo bar baz unique")
        await nts.index_text(str(ULID()), ch_b, nts.params_hash, "hello world test")
        await nts.index_text(str(ULID()), ch_c, nts.params_hash, "no numbers here at all")

        entries = [
            _search_meta_entry("/a.txt", ch_a),
            _search_meta_entry("/b.txt", ch_b),
            _search_meta_entry("/c.txt", ch_c),
        ]
        response = await nts.search_text(_req("foo", SearchType.REGEX, entries), [])

        assert {r.path for r in response.results} == {"/a.txt"}

    @pytest.mark.asyncio
    async def test_fulltext_ranked_results(self, pg_store):
        """RankedFulltext (Postgres): ts_rank orders higher-frequency matches above lower-frequency ones."""
        nts = pg_store.native_text_search()
        ch_high = _ch("python python python python high frequency")
        ch_low = _ch("python appears once only here low")
        # Must not contain the query token at all — an earlier draft said
        # "no python", which of course *contains* "python" and matched.
        ch_none = _ch("java scala kotlin rust go")

        await nts.index_text(str(ULID()), ch_high, nts.params_hash, "python python python python high frequency")
        await nts.index_text(str(ULID()), ch_low, nts.params_hash, "python appears once only here low")
        await nts.index_text(str(ULID()), ch_none, nts.params_hash, "java scala kotlin rust go")

        entries = [
            _search_meta_entry("/high.txt", ch_high),
            _search_meta_entry("/low.txt", ch_low),
            _search_meta_entry("/none.txt", ch_none),
        ]
        response = await nts.search_text(_req("python", SearchType.FULLTEXT, entries), [])

        paths = {r.path for r in response.results}
        assert "/none.txt" not in paths, "document without 'python' after stemming must be excluded"
        assert {"/high.txt", "/low.txt"} <= paths

        scores = {r.path: r.score for r in response.results}
        if "/high.txt" in scores and "/low.txt" in scores:
            assert scores["/high.txt"] >= scores["/low.txt"], (
                f"high-frequency doc score {scores['/high.txt']:.4f} must be >= "
                f"low-frequency doc score {scores['/low.txt']:.4f}"
            )

    @pytest.mark.asyncio
    async def test_fulltext_match_any_ranks_union(self, pg_store):
        """FulltextMatchAnyRanksUnion (Postgres): mode=ANY returns the union, both-terms doc ranks first.

        Corpus: "hello world" (matches one term) and "hello cloud bucket" (matches both).
        Query "hello cloud" in mode=ANY returns both; the both-terms doc ranks above the
        one-term doc.  Terms are ≥3 chars to stay consistent with the SQLite-compatible
        cross-backend corpus (Postgres itself handles short terms fine).
        """
        nts = pg_store.native_text_search()
        ch_one = _ch("hello world")
        ch_both = _ch("hello cloud bucket")
        await nts.index_text(str(ULID()), ch_one, nts.params_hash, "hello world")
        await nts.index_text(str(ULID()), ch_both, nts.params_hash, "hello cloud bucket")

        entries = [_search_meta_entry("/one.txt", ch_one), _search_meta_entry("/both.txt", ch_both)]
        response = await nts.search_text(
            _req("hello cloud", SearchType.FULLTEXT, entries, match_mode=FullTextMatchMode.ANY), []
        )

        paths = [r.path for r in response.results]
        assert set(paths) == {"/one.txt", "/both.txt"}, "ANY mode must return the union of per-term matches"
        assert paths.index("/both.txt") < paths.index("/one.txt"), (
            f"both-terms doc must rank above one-term doc; got order {paths}"
        )

    @pytest.mark.asyncio
    async def test_fulltext_match_all_requires_every_term(self, pg_store):
        """FulltextMatchAllRequiresEveryTerm (Postgres): mode=ALL returns only docs with every term.

        Same corpus as the ANY test; query "hello cloud" in mode=ALL returns only the
        both-terms doc ("hello world" lacks "cloud").
        """
        nts = pg_store.native_text_search()
        ch_one = _ch("hello world")
        ch_both = _ch("hello cloud bucket")
        await nts.index_text(str(ULID()), ch_one, nts.params_hash, "hello world")
        await nts.index_text(str(ULID()), ch_both, nts.params_hash, "hello cloud bucket")

        entries = [_search_meta_entry("/one.txt", ch_one), _search_meta_entry("/both.txt", ch_both)]
        response = await nts.search_text(
            _req("hello cloud", SearchType.FULLTEXT, entries, match_mode=FullTextMatchMode.ALL), []
        )

        assert {r.path for r in response.results} == {"/both.txt"}, "ALL mode must require every query term"

    @pytest.mark.asyncio
    async def test_ranked_fulltext_any_mode(self, pg_store):
        """RankedFulltextAnyMode (Postgres): a both-terms doc ranks above a one-term doc in ANY mode."""
        nts = pg_store.native_text_search()
        ch_both = _ch("hello cloud bucket")
        ch_one = _ch("hello world")
        await nts.index_text(str(ULID()), ch_both, nts.params_hash, "hello cloud bucket")
        await nts.index_text(str(ULID()), ch_one, nts.params_hash, "hello world")

        entries = [_search_meta_entry("/both.txt", ch_both), _search_meta_entry("/one.txt", ch_one)]
        response = await nts.search_text(
            _req("hello cloud", SearchType.FULLTEXT, entries, match_mode=FullTextMatchMode.ANY), []
        )

        paths = [r.path for r in response.results]
        assert set(paths) == {"/both.txt", "/one.txt"}
        assert paths.index("/both.txt") < paths.index("/one.txt"), (
            f"doc matching both terms must rank above doc matching one term; got {paths}"
        )
        scores = {r.path: r.score for r in response.results}
        assert scores["/both.txt"] >= scores["/one.txt"], (
            f"both-terms ts_rank {scores['/both.txt']:.4f} must be >= one-term {scores['/one.txt']:.4f}"
        )

    @pytest.mark.asyncio
    async def test_fulltext_single_term_any_equals_all(self, pg_store):
        """Single-term degenerate case (Postgres): one-term ANY == one-term ALL result set.

        With a single term the ANY construction reduces to one plainto_tsquery call —
        identical to ALL — so both modes return the same documents.
        """
        nts = pg_store.native_text_search()
        ch_hit = _ch("hello cloud bucket")
        ch_miss = _ch("java scala kotlin")
        await nts.index_text(str(ULID()), ch_hit, nts.params_hash, "hello cloud bucket")
        await nts.index_text(str(ULID()), ch_miss, nts.params_hash, "java scala kotlin")

        entries = [_search_meta_entry("/hit.txt", ch_hit), _search_meta_entry("/miss.txt", ch_miss)]

        any_resp = await nts.search_text(
            _req("hello", SearchType.FULLTEXT, entries, match_mode=FullTextMatchMode.ANY), []
        )
        all_resp = await nts.search_text(
            _req("hello", SearchType.FULLTEXT, entries, match_mode=FullTextMatchMode.ALL), []
        )

        assert {r.path for r in any_resp.results} == {"/hit.txt"}
        assert {r.path for r in any_resp.results} == {r.path for r in all_resp.results}, (
            "single-term ANY must match single-term ALL result set"
        )

    @pytest.mark.asyncio
    async def test_delete_text_artifacts_by_content_hash(self, pg_store):
        """delete_text_artifacts(content_hashes) removes indexed content so subsequent search returns nothing."""
        nts = pg_store.native_text_search()
        ch = _ch("delete me postgres content")
        await nts.index_text(str(ULID()), ch, nts.params_hash, "delete me postgres content")

        entries = [_search_meta_entry("/del.txt", ch)]

        before = await nts.search_text(_req("delete", SearchType.REGEX, entries), [])
        assert {r.path for r in before.results} == {"/del.txt"}, "must match before deletion"

        await nts.delete_text_artifacts([ch], [])

        after = await nts.search_text(_req("delete", SearchType.REGEX, entries), [])
        assert after.results == [], "must return nothing after content-hash deletion"

    @pytest.mark.asyncio
    async def test_delete_text_artifacts_retired_params_hash(self, pg_store):
        """delete_text_artifacts(retired_params_hashes=[...]) sweeps all records with that params_hash."""
        nts = pg_store.native_text_search()
        old_params = "oldparamshash1234"
        ch = _ch("retiring this profile content")

        # Index under a synthetic old params_hash.
        await nts.index_text(str(ULID()), ch, old_params, "retiring this profile content")

        # The entry was indexed under old_params — visible when queried with old_params manually.
        # Retire the old params_hash.
        await nts.delete_text_artifacts([], [old_params])

        # After retirement, querying the NTS (which uses nts.params_hash) returns nothing for this ch.
        entries = [_search_meta_entry("/retired.txt", ch)]
        after = await nts.search_text(_req("retiring", SearchType.REGEX, entries), [])
        assert after.results == [], "retired-params records must be deleted"

    @pytest.mark.asyncio
    async def test_live_referenced_content_never_swept(self, pg_store, tmp_path):
        """LiveReferencedContentNeverSwept (Postgres): GC honors a live reference under a real transaction.

        The reference check and the text-artifact deletion run in one BaseSqlMetadataStore
        transaction (the same path SQLite uses), so a live-referenced content_hash keeps both its
        blob and its text artifacts — the invariant the removed query-time existence re-check
        incidentally guarded.
        """
        from vfs.config import VFSConfig
        from vfs.gc import GarbageCollector
        from vfs.stores.local_blob import LocalFSBlobStore

        nts = pg_store.native_text_search()
        blob_store = LocalFSBlobStore(tmp_path / "blobs")

        content = b"keep me alive"
        content_hash = _ch("keep me alive")
        await blob_store.put(content_hash, content)
        await nts.index_text(str(ULID()), content_hash, nts.params_hash, content.decode())

        # A live (non-tombstone) version references the content.
        v = VersionMeta(
            id=str(ULID()),
            file_path="/live.txt",
            namespace_id="ns",
            version_number=1,
            content_hash=content_hash,
            size=len(content),
            created_at=_now(),
            created_by="tester",
        )
        await pg_store.put_version(v, expected_version=None)
        assert await pg_store.has_version_references(content_hash)

        gc = GarbageCollector(pg_store, blob_store, VFSConfig(audit_log_enabled=False))
        result = await gc.run()

        assert result.blobs_reclaimed == 0, "a live-referenced hash must not be swept"
        assert await blob_store.get(content_hash) == content, "blob for a live-referenced hash must survive"
        # The text artifact must survive too — search still finds the content.
        entry = _search_meta_entry("/live.txt", content_hash)
        resp = await nts.search_text(_req("keep", SearchType.REGEX, [entry]), [v.id])
        assert {r.path for r in resp.results} == {"/live.txt"}, "text artifact must survive GC"

    @pytest.mark.asyncio
    async def test_result_set_equivalent_to_brute_force(self, pg_store, sqlite_nts):
        """ResultSetEquivalentToBruteForce (Postgres leg): NTS and Python brute-force agree.

        Patterns include:
        - ``[0-9]+``: trigram-unfriendly — exercises sequential-scan fallback.
        - ``foo|barbaz``: alternation — GIN trigram index must not prune one branch.
        - ``cat|dogs``: two-branch alternation.
        - ``\\|``: escaped pipe matches literal ``|`` character.
        - ``order``: ordinary literal.

        Also formalizes the existing ALL-mode FULLTEXT behavior with an explicit
        cross-backend leg: the same multi-term query in ``mode=ALL`` returns the same path
        set on SQLite and Postgres (terms ≥3 chars for the SQLite FTS5 trigram floor).
        """
        nts = pg_store.native_text_search()

        documents = {
            "/digits.txt": "order 42 received on day 7",
            "/nodigits.txt": "no numbers here",
            "/mixed.txt": "version 3 of the spec has 12 items",
            "/alpha.txt": "only alphabetic content here",
            "/has_foo.txt": "this document contains foo only",
            "/has_barbaz.txt": "this document contains barbaz only",
            "/has_cat.txt": "the cat sat on the mat",
            "/has_dogs.txt": "dogs are friendly animals",
            "/has_pipe.txt": "a|b pipe character present",
        }

        entries = []
        for path, text in documents.items():
            ch = _ch(text)
            await nts.index_text(str(ULID()), ch, nts.params_hash, text)
            entries.append(_search_meta_entry(path, ch))

        patterns = [
            r"[0-9]+",  # trigram-unfriendly: no extractable literal trigrams
            r"order",  # plain literal
            r"foo|barbaz",  # alternation: must not false-negative foo-only documents
            r"cat|dogs",  # alternation: two branches
            r"\|",  # escaped pipe: literal '|', not alternation
        ]

        for pattern in patterns:
            response = await nts.search_text(_req(pattern, SearchType.REGEX, entries), [])
            pg_paths = {r.path for r in response.results}

            brute_paths = {p for p, text in documents.items() if re.search(pattern, text)}

            assert pg_paths == brute_paths, f"pattern {pattern!r}: Postgres={pg_paths} != brute-force={brute_paths}"

        # --- FULLTEXT mode=ALL cross-backend equivalence -------------------------------
        # Formalizes the existing ALL-mode contract: a multi-term query in mode=ALL returns
        # the same path set on both native backends. Corpus has one doc matching every term,
        # one matching a subset, and one matching neither, so the ALL result is non-trivial.
        ft_documents = {
            "/world.txt": "hello world",  # matches "hello" only → excluded by ALL
            "/cloud.txt": "hello cloud bucket",  # matches both "hello" and "cloud"
            "/other.txt": "random python content",  # matches neither
        }
        ft_entries = []
        for path, text in ft_documents.items():
            ch = _ch(text)
            await nts.index_text(str(ULID()), ch, nts.params_hash, text)
            await sqlite_nts.index_text(str(ULID()), ch, sqlite_nts.params_hash, text)
            ft_entries.append(_search_meta_entry(path, ch))

        ft_req = _req("hello cloud", SearchType.FULLTEXT, ft_entries, match_mode=FullTextMatchMode.ALL)
        pg_ft = await nts.search_text(ft_req, [])
        sqlite_ft = await sqlite_nts.search_text(ft_req, [])

        assert {r.path for r in pg_ft.results} == {r.path for r in sqlite_ft.results} == {"/cloud.txt"}, (
            f"ALL-mode FULLTEXT path set must match across backends; "
            f"pg={ {r.path for r in pg_ft.results} } sqlite={ {r.path for r in sqlite_ft.results} }"
        )

    @pytest.mark.asyncio
    async def test_any_mode_result_set_equivalent_across_backends(self, pg_store, sqlite_nts):
        """AnyModeResultSetEquivalentAcrossBackends: SQLite and Postgres agree on the ANY path set.

        Brute-force does NOT participate (there is no single-pass ranked-OR baseline); this
        compares the two native backends only.  Corpus has one doc matching both query
        terms, one matching each single term, and one matching neither, so the ANY union is
        non-trivial.  Asserts an identical path set across backends and that the both-terms
        doc outranks each one-term doc on each backend independently — scores are NOT
        compared across backends (BM25 vs ts_rank differ).  Terms are ≥3 chars for the
        SQLite FTS5 trigram floor.
        """
        from pyleak import no_task_leaks

        pg_nts = pg_store.native_text_search()

        documents = {
            "/both.txt": "hello cloud bucket",  # matches both "hello" and "cloud"
            "/hello.txt": "hello world",  # matches "hello" only
            "/cloud.txt": "cloud storage system",  # matches "cloud" only
            "/none.txt": "random python content",  # matches neither
        }

        entries = []
        for path, text in documents.items():
            ch = _ch(text)
            await pg_nts.index_text(str(ULID()), ch, pg_nts.params_hash, text)
            await sqlite_nts.index_text(str(ULID()), ch, sqlite_nts.params_hash, text)
            entries.append(_search_meta_entry(path, ch))

        req = _req("hello cloud", SearchType.FULLTEXT, entries, match_mode=FullTextMatchMode.ANY)

        # Act: run the same ANY-mode FULLTEXT query against both native backends.
        async with no_task_leaks(action="raise"):
            pg_resp = await pg_nts.search_text(req, [])
            sqlite_resp = await sqlite_nts.search_text(req, [])

        pg_paths = [r.path for r in pg_resp.results]
        sqlite_paths = [r.path for r in sqlite_resp.results]
        expected_union = {"/both.txt", "/hello.txt", "/cloud.txt"}

        assert set(pg_paths) == set(sqlite_paths) == expected_union, (
            f"ANY-mode path set must be identical across backends; pg={set(pg_paths)} sqlite={set(sqlite_paths)}"
        )

        # Monotonic ordering, asserted per backend (scores not compared across backends).
        for label, paths in (("postgres", pg_paths), ("sqlite", sqlite_paths)):
            assert paths.index("/both.txt") < paths.index("/hello.txt"), (
                f"{label}: both-terms doc must rank above the hello-only doc; got {paths}"
            )
            assert paths.index("/both.txt") < paths.index("/cloud.txt"), (
                f"{label}: both-terms doc must rank above the cloud-only doc; got {paths}"
            )
