"""Integration tests for MongoMetadataStore against a real MongoDB server.

These tests require a reachable MongoDB server. Provide its URI via the
``AIVFS_TEST_MONGO_URI`` environment variable, e.g.::

    AIVFS_TEST_MONGO_URI=mongodb://localhost:27017/aivfs

Start a local server with the Docker Compose fixture::

    docker compose -f tests/integration/docker-compose.yaml up -d mongo

The whole module is skipped when ``motor`` is not installed, the URI is unset, or the
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
from vfs.models import AuditEvent, FileMeta, Permission, RetentionPolicy, VersionMeta

_URI = os.environ.get("AIVFS_TEST_MONGO_URI")

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("motor") is None or not _URI,
    reason="requires motor and AIVFS_TEST_MONGO_URI pointing at a reachable MongoDB server",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest_asyncio.fixture
async def mongo_store():
    """A connected MongoMetadataStore against this xdist worker's own database.

    Each worker owns a database named ``aivfs_test_<worker>`` derived from the base URI, so
    parallel xdist workers never clobber each other. The worker database is dropped up front
    with a short-lived dedicated client (closed in ``finally``) for a clean slate, then the
    store is constructed and ``initialize()``-d exactly once. ``initialize()`` reassigns
    ``self._client`` without closing the prior one, so it must run only once per store
    instance to avoid leaking a Motor client/connection pool. Skips cleanly if the server is
    unreachable.
    """
    from motor.motor_asyncio import AsyncIOMotorClient
    from pymongo.errors import PyMongoError

    from vfs.stores.mongo_metadata import MongoMetadataStore

    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    test_db = f"aivfs_test_{worker}"

    # Swap the database segment of the base URI to the per-worker database.
    base = _URI.rstrip("/")
    # Strip any existing trailing /<db> path before appending the worker db.
    scheme, _, remainder = base.partition("://")
    host_part = remainder.split("/", 1)[0]
    worker_uri = f"{scheme}://{host_part}/{test_db}"

    # Drop the worker database for a clean slate with a dedicated short-lived client.
    cleanup_client = AsyncIOMotorClient(worker_uri)
    try:
        await cleanup_client.drop_database(test_db)
    except (PyMongoError, OSError) as exc:  # unreachable server / auth failure
        pytest.skip(f"MongoDB worker database unreachable at AIVFS_TEST_MONGO_URI: {exc}")
    finally:
        cleanup_client.close()

    store = MongoMetadataStore(worker_uri)
    try:
        await store.initialize()
    except (PyMongoError, OSError) as exc:  # unreachable server / auth failure
        await store.close()
        pytest.skip(f"MongoDB worker database unreachable at AIVFS_TEST_MONGO_URI: {exc}")

    yield store

    try:
        if store._client is not None:
            await store._client.drop_database(test_db)
    except (PyMongoError, OSError):
        pass  # best-effort teardown; the next run drops it anyway
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_round_trip_subdocuments(mongo_store):
    """MongoAdapterRoundTrip: file + version with non-empty search_meta, an audit event with
    non-empty detail, and a permission with operations all round-trip, with search_meta and
    detail stored as native dict subdocuments (not JSON strings)."""
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
    await mongo_store.put_version(version, expected_version=None)

    file_meta = await mongo_store.get_file("ns1", "/src/a.py")
    assert file_meta is not None
    assert file_meta.path == "/src/a.py"
    assert file_meta.current_version_number == 1

    fetched_version = await mongo_store.get_version("ns1", "/src/a.py", 1)
    assert fetched_version is not None
    assert fetched_version == version
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
    await mongo_store.append_audit_event(event)

    # Read the raw documents back through the underlying collections to assert search_meta
    # and detail are stored as native dict subdocuments, not serialized JSON strings.
    version_doc = await mongo_store._db.versions.find_one({"id": version.id})
    assert version_doc is not None
    assert isinstance(version_doc["search_meta"], dict)
    assert version_doc["search_meta"] == search_meta

    detail_doc = await mongo_store._db.audit_events.find_one({"event_id": event.event_id})
    assert detail_doc is not None
    assert isinstance(detail_doc["detail"], dict)
    assert detail_doc["detail"] == detail

    permission = Permission(
        id=str(ULID()),
        principal_id="principal1",
        namespace_id="ns1",
        path_prefix="/src/",
        operations={"read", "write"},
        created_at=now,
    )
    await mongo_store.set_permission(permission)
    assert await mongo_store.check_permission("principal1", "ns1", "/src/a.py", "read") is True
    assert await mongo_store.check_permission("principal1", "ns1", "/src/a.py", "write") is True
    assert await mongo_store.check_permission("principal1", "ns1", "/src/a.py", "delete") is False


@pytest.mark.asyncio
async def test_cas_conflict_at_write_site(mongo_store):
    """MongoCASConflict: a file advanced to version 5 rejects put_version(expected=3) via
    find_one_and_update returning no document, leaves the pointer at 5, and inserts no orphan
    version document."""
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
        await mongo_store.put_version(version, expected_version=None if i == 1 else i - 1)

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
        await mongo_store.put_version(stale, expected_version=3)

    file_meta = await mongo_store.get_file("ns1", "/a.py")
    assert file_meta.current_version_number == 5
    versions = await mongo_store.list_versions("ns1", "/a.py", limit=100)
    assert len(versions) == 5
    assert stale.id not in {v.id for v in versions}


@pytest.mark.asyncio
async def test_put_version_insert_failure_leaves_pointer_unadvanced(mongo_store):
    """A version-insert failure on the CAS branch must NOT advance the pointer (insert-first
    ordering). Pre-insert a version doc colliding on the unique
    (namespace_id, file_path, version_number) index so the next real insert raises
    DuplicateKeyError before any pointer move; the file must remain at version 1."""
    import pymongo.errors

    # Establish version 1.
    await mongo_store.put_version(_version("ns1", "/a.py", 1, content_hash="h1"), expected_version=None)

    # Pre-insert a version doc with the SAME (namespace_id, file_path, version_number=2) so the
    # next put_version insert collides on the unique index.
    decoy = _version("ns1", "/a.py", 2, content_hash="decoy")
    await mongo_store._db.versions.insert_one(mongo_store._version_to_doc(decoy))

    conflicting = _version("ns1", "/a.py", 2, content_hash="h2")
    with pytest.raises(pymongo.errors.DuplicateKeyError):
        await mongo_store.put_version(conflicting, expected_version=1)

    # The pointer never advanced: insert-first means the failed insert ran before the CAS.
    file_meta = await mongo_store.get_file("ns1", "/a.py")
    assert file_meta.current_version_number == 1


@pytest.mark.asyncio
async def test_audit_event_unique_event_id_enforced(mongo_store):
    """Appending two AuditEvents with the same event_id raises DuplicateKeyError — the unique
    index on audit_events.event_id (a domain primary key) is enforced."""
    import pymongo.errors

    event_id = str(ULID())
    event = AuditEvent(
        event_id=event_id,
        timestamp=_now(),
        namespace_id="ns1",
        principal_id="principal1",
        operation="write",
        path="/a.py",
        version_id="v1",
        detail={},
    )
    await mongo_store.append_audit_event(event)

    duplicate = AuditEvent(
        event_id=event_id,
        timestamp=_now(),
        namespace_id="ns1",
        principal_id="principal1",
        operation="delete",
        path="/a.py",
        version_id="v2",
        detail={},
    )
    with pytest.raises(pymongo.errors.DuplicateKeyError):
        await mongo_store.append_audit_event(duplicate)


@pytest.mark.asyncio
async def test_set_name_duplicate_raises_conflict_and_store_stays_usable(mongo_store):
    """A different entity claiming an existing display name raises ConflictError and the
    store stays usable afterward: the original mapping holds, new non-conflicting names are
    accepted, and same-entity renames still work."""
    await mongo_store.set_name("namespace", "id_A", "shared")

    with pytest.raises(ConflictError):
        await mongo_store.set_name("namespace", "id_B", "shared")

    # The store is still usable after the rejected write (no transaction to poison).
    assert await mongo_store.resolve_name("namespace", "shared") == "id_A"
    await mongo_store.set_name("namespace", "id_C", "other")
    assert await mongo_store.resolve_name("namespace", "other") == "id_C"

    # The legitimate same-entity rename still works.
    await mongo_store.set_name("namespace", "id_A", "renamed")
    assert await mongo_store.resolve_name("namespace", "renamed") == "id_A"


def _file(ns: str, path: str, *, is_deleted: bool = False) -> FileMeta:
    now = _now()
    return FileMeta(
        namespace_id=ns,
        path=path,
        current_version_id="v1",
        current_version_number=1,
        created_at=now,
        updated_at=now,
        is_deleted=is_deleted,
    )


def _version(ns: str, path: str, num: int, *, content_hash: str = "hash1", is_tombstone: bool = False) -> VersionMeta:
    return VersionMeta(
        id=str(ULID()),
        file_path=path,
        namespace_id=ns,
        version_number=num,
        content_hash=content_hash,
        size=42,
        created_at=_now(),
        created_by="principal1",
        is_tombstone=is_tombstone,
    )


@pytest.mark.asyncio
async def test_list_dir_non_recursive_and_recursive(mongo_store):
    """list_dir uses a Mongo regex prefix scan plus a Python remainder check: non-recursive
    excludes deeper paths (remainder containing '/') and includes only direct children;
    recursive=True includes nested paths. Mirrors the SQLite list_dir semantics."""
    for p in ["/src/a.py", "/src/b.py", "/src/sub/c.py"]:
        await mongo_store.put_file(_file("ns1", p))

    non_recursive = await mongo_store.list_dir("ns1", "/src/", recursive=False)
    assert {r.path for r in non_recursive} == {"/src/a.py", "/src/b.py"}

    recursive = await mongo_store.list_dir("ns1", "/src/", recursive=True)
    assert {r.path for r in recursive} == {"/src/a.py", "/src/b.py", "/src/sub/c.py"}


@pytest.mark.asyncio
async def test_list_dir_excludes_deleted(mongo_store):
    """list_dir must not return files with is_deleted=True (the regex query filters them)."""
    await mongo_store.put_file(_file("ns1", "/src/live.py"))
    await mongo_store.put_file(_file("ns1", "/src/gone.py", is_deleted=True))

    results = await mongo_store.list_dir("ns1", "/src/")
    assert {r.path for r in results} == {"/src/live.py"}


@pytest.mark.asyncio
async def test_check_permission_most_specific_prefix_wins(mongo_store):
    """check_permission sorts overlapping prefixes by length and the longest match wins.
    With '/' granting read and '/src/' granting read+write, a path under '/src/' resolves to
    the '/src/' rule (write allowed); a path outside '/src/' falls back to '/' (write
    denied). Mirrors the SQLite most-specific semantics."""
    await mongo_store.set_permission(
        Permission(
            id=str(ULID()),
            principal_id="p1",
            namespace_id="ns1",
            path_prefix="/",
            operations={"read"},
            created_at=_now(),
        )
    )
    await mongo_store.set_permission(
        Permission(
            id=str(ULID()),
            principal_id="p1",
            namespace_id="ns1",
            path_prefix="/src/",
            operations={"read", "write"},
            created_at=_now(),
        )
    )

    # Under /src/: the longer (most-specific) prefix wins, so write is allowed.
    assert await mongo_store.check_permission("p1", "ns1", "/src/file.py", "write") is True
    assert await mongo_store.check_permission("p1", "ns1", "/src/file.py", "read") is True
    # Outside /src/: only the broad "/" rule (read-only) matches, so write is denied.
    assert await mongo_store.check_permission("p1", "ns1", "/other/file.py", "write") is False
    assert await mongo_store.check_permission("p1", "ns1", "/other/file.py", "read") is True


@pytest.mark.asyncio
async def test_list_reclaimable_versions(mongo_store):
    """list_reclaimable_versions sorts non-tombstone versions newest-first and returns the
    excess beyond max_recent_versions, except version 1 is retained when
    keep_first_version is true. With 4 versions and N=1: keep v4 (most recent), keep v1
    (first), reclaim v2 and v3. Mirrors the SQLite logic."""
    for i in range(1, 5):
        await mongo_store.put_version(
            _version("ns1", "/a.py", i, content_hash=f"h{i}"),
            expected_version=None if i == 1 else i - 1,
        )

    policy = RetentionPolicy(max_recent_versions=1, keep_first_version=True)
    reclaimable = await mongo_store.list_reclaimable_versions(policy, "ns1")
    assert {v.version_number for v in reclaimable} == {2, 3}


@pytest.mark.asyncio
async def test_get_version_latest(mongo_store):
    """get_version(version_number=None) returns the newest version via sort on version_number
    descending. Mirrors the SQLite latest-version semantics."""
    await mongo_store.put_version(_version("ns1", "/a.py", 1, content_hash="h1"), expected_version=None)
    await mongo_store.put_version(_version("ns1", "/a.py", 2, content_hash="h2"), expected_version=1)

    latest = await mongo_store.get_version("ns1", "/a.py")
    assert latest is not None
    assert latest.version_number == 2
    assert latest.content_hash == "h2"
