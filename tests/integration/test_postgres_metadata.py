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
import importlib.util
import os

import pytest
import pytest_asyncio
from ulid import ULID

from vfs.errors import ConflictError
from vfs.models import AuditEvent, FileMeta, Permission, VersionMeta

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

    yield str(base_url.set(database=test_db))

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
