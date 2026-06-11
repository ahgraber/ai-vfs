"""Integration tests: VersionCollisionError translation on Postgres and Mongo adapters.

These tests verify that concurrent no-CAS writes on real Postgres and Mongo stores
raise VersionCollisionError (not a raw IntegrityError / DuplicateKeyError), matching
the behaviour already tested for SQLite in tests/unit/test_boundary_hardening.py.

The SQLite leg is included here for cross-adapter completeness; it requires no Docker.
The Postgres and Mongo legs skip cleanly when their driver or service is unavailable.

Run just the SQLite leg:
    uv run pytest tests/integration/test_boundary_hardening.py -k sqlite -q

Run all legs (requires Docker):
    docker compose -f tests/integration/docker-compose.yaml up -d postgres mongo
    AIVFS_TEST_POSTGRES_DSN=postgresql://aivfs:aivfs@localhost:5432/aivfs \\
    AIVFS_TEST_MONGO_URI=mongodb://localhost:27017/aivfs \\
    uv run pytest tests/integration/test_boundary_hardening.py -q
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timezone
import importlib.util
import os

import pytest
import pytest_asyncio
from ulid import ULID

from vfs.errors import VersionCollisionError
from vfs.models import VersionMeta
from vfs.stores.sqlite_metadata import SQLiteMetadataStore

_POSTGRES_DSN = os.environ.get("AIVFS_TEST_POSTGRES_DSN")
_MONGO_URI = os.environ.get("AIVFS_TEST_MONGO_URI")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _version(ns: str, path: str, num: int, content_hash: str = "h1") -> VersionMeta:
    return VersionMeta(
        id=str(ULID()),
        file_path=path,
        namespace_id=ns,
        version_number=num,
        content_hash=content_hash,
        size=4,
        created_at=_now(),
        created_by="p1",
    )


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
    from sqlalchemy import text
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import SQLAlchemyError
    from sqlalchemy.ext.asyncio import create_async_engine

    from vfs.stores.postgres_metadata import PostgresMetadataStore

    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    base_url = make_url(_POSTGRES_DSN)
    maint_url = base_url.set(database="postgres", drivername="postgresql+asyncpg")
    db_name = f"aivfs_bh_test_{worker}"
    # str(URL) masks the password as "***"; render_as_string(hide_password=False) exposes it
    # for the adapter so asyncpg does not receive a literal three-asterisk password string.
    worker_dsn = base_url.set(database=db_name).render_as_string(hide_password=False)

    engine = create_async_engine(maint_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
            await conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    except SQLAlchemyError as exc:
        pytest.skip(f"cannot provision Postgres test database: {exc}")
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
    test_db = f"aivfs_bh_test_{worker}"
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


async def _assert_version_collision_translation(store) -> None:
    """Seed a file at version 1, insert version 2, then attempt to insert
    another version 2 — verifying the store translates the unique-constraint
    violation into VersionCollisionError, not a raw driver exception."""
    await store.put_version(_version("ns", "/a.txt", 1, "h1"), expected_version=None)
    await store.put_version(_version("ns", "/a.txt", 2, "h2"), expected_version=None)

    duplicate = VersionMeta(
        id=str(ULID()),
        file_path="/a.txt",
        namespace_id="ns",
        version_number=2,
        content_hash="h_dup",
        size=4,
        created_at=_now(),
        created_by="racer",
    )
    with pytest.raises(VersionCollisionError):
        await store.put_version(duplicate, expected_version=None)


@pytest.mark.asyncio
async def test_version_collision_translation_sqlite(sqlite_store):
    """SQLite: duplicate no-CAS version_number raises VersionCollisionError."""
    await _assert_version_collision_translation(sqlite_store)


@pytest.mark.asyncio
async def test_version_collision_translation_postgres(postgres_store):
    """Postgres: duplicate no-CAS version_number raises VersionCollisionError.

    Requires AIVFS_TEST_POSTGRES_DSN and the ``postgres`` extra.
    """
    await _assert_version_collision_translation(postgres_store)


@pytest.mark.asyncio
async def test_version_collision_translation_mongo(mongo_store):
    """Mongo: duplicate no-CAS version_number raises VersionCollisionError.

    Requires AIVFS_TEST_MONGO_URI and the ``mongo`` extra.
    """
    await _assert_version_collision_translation(mongo_store)
