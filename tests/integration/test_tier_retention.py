"""Integration test: tier-based retention produces identical results across all three adapters.

ReclamationIdenticalAcrossAdapters: given the same fixed version set and RetentionPolicy,
the tier evaluator must yield the same reclaimed version-ID set when run against SQLite,
PostgreSQL, and MongoDB.

The SQLite leg runs as a pure unit test (no Docker required).  The Postgres and Mongo legs
require the same environment variables and Docker Compose services used by the other
integration tests in this directory.  The whole module is designed so that missing drivers
or unreachable services cause individual parametrised cases to be skipped, not the whole file.

Run just the SQLite leg:
    uv run pytest tests/integration/test_tier_retention.py -k sqlite -q

Run all legs (requires Docker):
    docker compose -f tests/integration/docker-compose.yaml up -d postgres mongo
    AIVFS_TEST_POSTGRES_DSN=postgresql://aivfs:aivfs@localhost:5432/aivfs \\
    AIVFS_TEST_MONGO_URI=mongodb://localhost:27017/aivfs \\
    uv run pytest tests/integration/test_tier_retention.py -q
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
import importlib.util
import os
from typing import AsyncIterator

import pytest
import pytest_asyncio
from ulid import ULID

from vfs.gc import GarbageCollector, evaluate_tier_retention
from vfs.models import RetentionPolicy, RetentionTier, VersionMeta
from vfs.stores.sqlite_metadata import SQLiteMetadataStore

_POSTGRES_DSN = os.environ.get("AIVFS_TEST_POSTGRES_DSN")
_MONGO_URI = os.environ.get("AIVFS_TEST_MONGO_URI")

# ---------------------------------------------------------------------------
# Fixed dataset
# ---------------------------------------------------------------------------

_NOW = datetime(2024, 6, 1, 0, 0, 0, tzinfo=timezone.utc)

# 12 versions: 2 per hourly window over 6 windows.  The oldest version in each
# window is the expected tier survivor (for keep_every=1h).
# Window 5 (age 5–6h): v1 (oldest overall, survivor), v2
# Window 4 (age 4–5h): v3 (survivor), v4
# Window 3 (age 3–4h): v5 (survivor), v6
# Window 2 (age 2–3h): v7 (survivor), v8
# Window 1 (age 1–2h): v9 (survivor), v10
# Window 0 (age 0–1h): v11 (survivor), v12 (current, newest)
_FIXED_VERSIONS: list[VersionMeta] = []
_vnum = 1
for _win in range(5, -1, -1):  # window 5 to 0; oldest windows first so v1 is oldest
    for _k in (1, 0):  # k=1 is older (larger age), k=0 is newer (smaller age)
        _age_secs = _win * 3600 + _k * 1200 + 300  # two versions ~20 min apart within the window
        _FIXED_VERSIONS.append(
            VersionMeta(
                id=str(ULID()),
                file_path="/cross/file.txt",
                namespace_id="ns",
                version_number=_vnum,
                content_hash=f"hash{_vnum:02d}",
                size=10,
                created_at=_NOW - timedelta(seconds=_age_secs),
                created_by="tester",
            )
        )
        _vnum += 1

_POLICY = RetentionPolicy(
    max_recent_versions=0,
    tiers=[RetentionTier(max_age=timedelta(hours=24), keep_every=timedelta(hours=1))],
    keep_first_version=True,
    keep_current_version=True,
)

# Pre-compute the expected reclaimable set from the pure evaluator (no store).
_CURRENT_VERSION_ID = max(_FIXED_VERSIONS, key=lambda v: v.version_number).id
_EXPECTED_RECLAIMABLE = evaluate_tier_retention(_FIXED_VERSIONS, _POLICY, _NOW, _CURRENT_VERSION_ID)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _populate_and_collect(store) -> set[str]:
    """Seed the fixed version set into *store*, run tier GC, and return reclaimed IDs.

    Uses a throw-away LocalFSBlobStore (blob GC not under test here).
    """
    import tempfile

    from vfs.config import VFSConfig
    from vfs.stores.local_blob import LocalFSBlobStore

    with tempfile.TemporaryDirectory() as tmp:
        blob = LocalFSBlobStore(tmp)
        config = VFSConfig(retention_max_recent=50, audit_log_enabled=False)
        gc = GarbageCollector(store, blob, config)

        # Insert in version_number order, using CAS after the first.
        sorted_versions = sorted(_FIXED_VERSIONS, key=lambda v: v.version_number)
        for i, v in enumerate(sorted_versions):
            ev = None if i == 0 else i
            await store.put_version(v, expected_version=ev)

        # Run the tier GC with the fixed reference time.
        await gc._tier_version_gc("ns", _POLICY, now=_NOW)

        # Compute reclaimed = original IDs minus remaining IDs.
        remaining = await store.list_versions("ns", "/cross/file.txt", limit=100)
        remaining_ids = {v.id for v in remaining}
        original_ids = {v.id for v in _FIXED_VERSIONS}
        return original_ids - remaining_ids


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def sqlite_store():
    store = SQLiteMetadataStore(":memory:")
    await store.initialize()
    yield store
    await store.close()


@pytest_asyncio.fixture
async def postgres_store():
    if importlib.util.find_spec("asyncpg") is None or not _POSTGRES_DSN:
        pytest.skip("requires asyncpg and AIVFS_TEST_POSTGRES_DSN")
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import SQLAlchemyError
    from sqlalchemy.ext.asyncio import create_async_engine

    from vfs.stores.postgres_metadata import PostgresMetadataStore

    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    base_url = make_url(_POSTGRES_DSN)
    maint_url = base_url.set(database="postgres", drivername="postgresql+asyncpg")
    db_name = f"aivfs_tier_test_{worker}"
    # str(URL) masks the password as "***"; render_as_string(hide_password=False) exposes it
    # for the adapter so asyncpg does not receive a literal three-asterisk password string.
    worker_dsn = base_url.set(database=db_name).render_as_string(hide_password=False)

    engine = create_async_engine(maint_url, isolation_level="AUTOCOMMIT")
    from sqlalchemy import text

    try:
        async with engine.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
            await conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    except (SQLAlchemyError, OSError) as exc:  # OSError: server unreachable (connection refused)
        pytest.skip(f"cannot provision Postgres tier-test database: {exc}")
    finally:
        await engine.dispose()

    store = PostgresMetadataStore(worker_dsn)
    try:
        await store.initialize()
    except Exception as exc:
        await store.close()
        pytest.skip(f"Postgres store initialization failed: {exc}")

    yield store

    await store.close()
    with contextlib.suppress(Exception):
        engine2 = create_async_engine(maint_url, isolation_level="AUTOCOMMIT")
        async with engine2.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
        await engine2.dispose()


@pytest_asyncio.fixture
async def mongo_store():
    if importlib.util.find_spec("motor") is None or not _MONGO_URI:
        pytest.skip("requires motor and AIVFS_TEST_MONGO_URI")
    from motor.motor_asyncio import AsyncIOMotorClient
    from pymongo.errors import PyMongoError

    from vfs.stores.mongo_metadata import MongoMetadataStore

    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    test_db = f"aivfs_tier_test_{worker}"
    base = _MONGO_URI.rstrip("/")
    scheme, _, remainder = base.partition("://")
    host_part = remainder.split("/", 1)[0]
    worker_uri = f"{scheme}://{host_part}/{test_db}"

    cleanup_client = AsyncIOMotorClient(worker_uri)
    try:
        await cleanup_client.drop_database(test_db)
    except (PyMongoError, OSError) as exc:
        pytest.skip(f"MongoDB unreachable: {exc}")
    finally:
        cleanup_client.close()

    store = MongoMetadataStore(worker_uri)
    try:
        await store.initialize()
    except (PyMongoError, OSError) as exc:
        await store.close()
        pytest.skip(f"MongoDB store initialization failed: {exc}")

    yield store

    with contextlib.suppress(Exception):
        if store._client is not None:
            await store._client.drop_database(test_db)
    await store.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_iter_versions_for_gc_order_sqlite(sqlite_store):
    """iter_versions_for_gc on SQLite returns non-tombstone versions ordered by (created_at, version_number)."""
    for i, v in enumerate(sorted(_FIXED_VERSIONS, key=lambda v: v.version_number)):
        await sqlite_store.put_version(v, expected_version=None if i == 0 else i)

    collected = [v async for v in sqlite_store.iter_versions_for_gc("ns", "/cross/file.txt")]
    # Must be in ascending created_at order (oldest first).
    for a, b in zip(collected, collected[1:]):
        assert (a.created_at, a.version_number) <= (b.created_at, b.version_number)
    assert len(collected) == len(_FIXED_VERSIONS)


@pytest.mark.asyncio
async def test_reclamation_sqlite(sqlite_store):
    """SQLite tier GC reclaims the same version IDs as the pure evaluator predicts."""
    reclaimed = await _populate_and_collect(sqlite_store)
    assert reclaimed == _EXPECTED_RECLAIMABLE, (
        f"SQLite reclaimed {len(reclaimed)} versions; expected {len(_EXPECTED_RECLAIMABLE)}"
    )


@pytest.mark.asyncio
async def test_reclamation_postgres(postgres_store):
    """Postgres tier GC reclaims the same version IDs as the pure evaluator predicts.

    Requires AIVFS_TEST_POSTGRES_DSN and the ``postgres`` extra.
    """
    reclaimed = await _populate_and_collect(postgres_store)
    assert reclaimed == _EXPECTED_RECLAIMABLE, (
        f"Postgres reclaimed {len(reclaimed)} versions; expected {len(_EXPECTED_RECLAIMABLE)}"
    )


@pytest.mark.asyncio
async def test_reclamation_mongo(mongo_store):
    """Mongo tier GC reclaims the same version IDs as the pure evaluator predicts.

    Requires AIVFS_TEST_MONGO_URI and the ``mongo`` extra.
    """
    reclaimed = await _populate_and_collect(mongo_store)
    assert reclaimed == _EXPECTED_RECLAIMABLE, (
        f"Mongo reclaimed {len(reclaimed)} versions; expected {len(_EXPECTED_RECLAIMABLE)}"
    )


@pytest.mark.asyncio
async def test_reclamation_identical_across_adapters(sqlite_store, postgres_store, mongo_store):
    """ReclamationIdenticalAcrossAdapters: all three adapters yield the same reclaimed set.

    This test only runs when all three stores are available (Postgres/Mongo need Docker).
    """
    sqlite_reclaimed = await _populate_and_collect(sqlite_store)
    postgres_reclaimed = await _populate_and_collect(postgres_store)
    mongo_reclaimed = await _populate_and_collect(mongo_store)

    assert sqlite_reclaimed == postgres_reclaimed == mongo_reclaimed, (
        f"Adapter disagreement: SQLite={len(sqlite_reclaimed)}, "
        f"Postgres={len(postgres_reclaimed)}, Mongo={len(mongo_reclaimed)}"
    )
