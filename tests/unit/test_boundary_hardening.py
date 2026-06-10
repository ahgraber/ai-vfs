"""Tests for boundary-hardening fixes.

Covers four confirmed bugs:
  1. Canonical-path enforcement at the VFS boundary.
  2. Segment-aware permission prefix matching.
  3. LIKE wildcard escaping in SQL prefix queries.
  4. Concurrent no-CAS write retry on VersionCollisionError.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone
import posixpath

from pyleak import no_task_leaks
import pytest
import pytest_asyncio

from vfs.config import VFSConfig
from vfs.errors import ConflictError, VersionCollisionError
from vfs.models import FileMeta, Permission, VersionMeta
from vfs.session import resolve_path
from vfs.stores.sqlite_metadata import SQLiteMetadataStore
from vfs.vfs import VFS, _require_canonical

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _perm(ns: str, principal: str, prefix: str, ops: set[str], pid: str = "pid1") -> Permission:
    return Permission(
        id=pid,
        principal_id=principal,
        namespace_id=ns,
        path_prefix=prefix,
        operations=ops,
        created_at=_now(),
    )


def _version(ns: str, path: str, num: int, *, content_hash: str = "h1") -> VersionMeta:
    from ulid import ULID

    return VersionMeta(
        id=str(ULID()),
        file_path=path,
        namespace_id=ns,
        version_number=num,
        content_hash=content_hash,
        size=10,
        created_at=_now(),
        created_by="writer",
    )


@pytest_asyncio.fixture
async def sqlite_store():
    store = SQLiteMetadataStore(":memory:")
    await store.initialize()
    yield store
    await store.close()


@pytest_asyncio.fixture
async def vfs_instance(tmp_path):
    config = VFSConfig(
        metadata_store_uri=f"sqlite:///{tmp_path / 'test.db'}",
        blob_store_uri=f"file:///{tmp_path / 'blobs'}/",
        otel_enabled=False,
        audit_log_enabled=False,
    )
    vfs = VFS(config)
    await vfs.initialize()
    yield vfs
    await vfs.close()


# ---------------------------------------------------------------------------
# Issue 1 — Canonical absolute paths
# ---------------------------------------------------------------------------


class TestRequireCanonical:
    """AbsolutePathsOnly/DotDotPathRejected, DoubleSlashRejected,
    TrailingSlashDirectoryArgAccepted — unit tests for the guard function."""

    def test_relative_path_raises(self):
        with pytest.raises(ValueError, match="absolute"):
            _require_canonical("relative/path")

    def test_relative_dot_raises(self):
        with pytest.raises(ValueError, match="absolute"):
            _require_canonical("./something")

    def test_dotdot_segment_raises(self):
        with pytest.raises(ValueError, match="canonical"):
            _require_canonical("/public/../secret/x")

    def test_dot_segment_raises(self):
        with pytest.raises(ValueError, match="canonical"):
            _require_canonical("/foo/./bar")

    def test_double_slash_middle_raises(self):
        with pytest.raises(ValueError, match="canonical"):
            _require_canonical("/foo//bar")

    def test_root_accepted(self):
        _require_canonical("/")  # no exception

    def test_bare_absolute_path_accepted(self):
        _require_canonical("/foo/bar/baz")  # no exception

    def test_trailing_slash_accepted(self):
        _require_canonical("/src/")  # no exception

    def test_nested_trailing_slash_accepted(self):
        _require_canonical("/workspace/src/")  # no exception


class TestVFSCanonicalEnforcement:
    """VFS operations reject non-canonical paths before any permission or storage access."""

    @pytest.mark.asyncio
    async def test_stat_rejects_dotdot(self, vfs_instance):
        with pytest.raises(ValueError):
            await vfs_instance.stat("ns", "/public/../secret", principal_id="p")

    @pytest.mark.asyncio
    async def test_write_rejects_dotdot(self, vfs_instance):
        with pytest.raises(ValueError):
            await vfs_instance.write("ns", "/a/../b", b"data", principal_id="p")

    @pytest.mark.asyncio
    async def test_read_rejects_double_slash(self, vfs_instance):
        with pytest.raises(ValueError):
            await vfs_instance.read("ns", "/foo//bar", principal_id="p")

    @pytest.mark.asyncio
    async def test_list_rejects_dotdot(self, vfs_instance):
        with pytest.raises(ValueError):
            await vfs_instance.list("ns", "/src/../etc/", principal_id="p")

    @pytest.mark.asyncio
    async def test_delete_rejects_dotdot(self, vfs_instance):
        with pytest.raises(ValueError):
            await vfs_instance.delete("ns", "/a/../b", principal_id="p")

    @pytest.mark.asyncio
    async def test_copy_rejects_dotdot_src(self, vfs_instance):
        with pytest.raises(ValueError):
            await vfs_instance.copy("ns", "/a/../b", "/c", principal_id="p")

    @pytest.mark.asyncio
    async def test_copy_rejects_dotdot_dst(self, vfs_instance):
        with pytest.raises(ValueError):
            await vfs_instance.copy("ns", "/a", "/b/../c", principal_id="p")

    @pytest.mark.asyncio
    async def test_move_rejects_dotdot_src(self, vfs_instance):
        with pytest.raises(ValueError):
            await vfs_instance.move("ns", "/a/../b", "/c", principal_id="p")

    @pytest.mark.asyncio
    async def test_list_trailing_slash_accepted(self, vfs_instance):
        """A directory-style path like /src/ must not be rejected."""
        # No permission is set up, but the ValueError must not be the reason for the stop.
        # We expect PermissionDeniedError or an empty list, not ValueError.
        from vfs.errors import PermissionDeniedError

        with contextlib.suppress(PermissionDeniedError, Exception):
            try:
                await vfs_instance.list("ns", "/src/", principal_id="p")
            except ValueError:
                pytest.fail("ValueError raised for /src/ — trailing slash should be accepted")


class TestGrantRejectsNonCanonicalPrefix:
    """grant() must validate path_prefix is canonical."""

    @pytest.mark.asyncio
    async def test_grant_rejects_dotdot_prefix(self, vfs_instance):
        admin = await vfs_instance.create_principal("admin")
        ns = await vfs_instance.create_namespace("ns", admin.id)
        await vfs_instance.bootstrap_admin(admin.id, ns.id)
        with pytest.raises(ValueError):
            await vfs_instance.grant(admin.id, "other", ns.id, "/a/../b", {"read"})

    @pytest.mark.asyncio
    async def test_grant_accepts_canonical_prefix(self, vfs_instance):
        admin = await vfs_instance.create_principal("admin2")
        ns = await vfs_instance.create_namespace("ns2", admin.id)
        await vfs_instance.bootstrap_admin(admin.id, ns.id)
        other = await vfs_instance.create_principal("other2")
        # Should not raise.
        await vfs_instance.grant(admin.id, other.id, ns.id, "/workspace/", {"read"})


class TestSessionResolvePathIsCanonical:
    """Session.resolve_path output always satisfies the canonical rule."""

    @pytest.mark.parametrize(
        "cwd,path",
        [
            ("/", "../../etc"),
            ("/foo/", "../bar"),
            ("/workspace/", "./src/"),
            ("/a/b/c/", "../../.."),
            ("/", "src/app/../lib/"),
            ("/workspace/", ""),  # empty relative → stays at cwd; normpath handles it
        ],
    )
    def test_resolve_is_canonical(self, cwd: str, path: str):
        result = resolve_path(cwd, path)
        check = result[:-1] if (result.endswith("/") and result != "/") else result
        assert check == posixpath.normpath(check), f"resolve_path({cwd!r}, {path!r}) = {result!r} is not canonical"

    def test_resolve_absolute_path_unchanged_and_canonical(self):
        result = resolve_path("/src/", "/data/file.txt")
        check = result[:-1] if result.endswith("/") and result != "/" else result
        assert check == posixpath.normpath(check)


# ---------------------------------------------------------------------------
# NIT — _require_canonical and _prefix_matches copy-parity
# ---------------------------------------------------------------------------


class TestHelperParity:
    """The three _require_canonical copies (vfs.py, sql_metadata.py, mongo_metadata.py)
    and the two _prefix_matches copies (sql_metadata.py, mongo_metadata.py) must
    behave identically on every edge case."""

    @pytest.mark.parametrize(
        "path,expect_error",
        [
            ("/", False),
            ("/foo", False),
            ("/foo/bar", False),
            ("/foo/", False),
            ("/foo/bar/", False),
            # Root with trailing slash is just "/"
            (".", True),
            ("", True),
            ("relative/path", True),
            ("/a/.", True),
            ("/a/../b", True),
            ("/a//b", True),
            ("/./foo", True),
        ],
    )
    def test_require_canonical_parity(self, path: str, expect_error: bool):
        """All available _require_canonical copies raise ValueError on the same inputs.

        The mongo copy requires motor; it is included when motor is installed and
        omitted with a note when not, so the test always runs for vfs + sql.
        """
        import importlib.util

        from vfs.stores.sql_metadata import _require_canonical as rc_sql
        from vfs.vfs import _require_canonical as rc_vfs

        impls: list[tuple[str, object]] = [("vfs", rc_vfs), ("sql", rc_sql)]
        if importlib.util.find_spec("motor") is not None:
            from vfs.stores.mongo_metadata import _require_canonical as rc_mongo

            impls.append(("mongo", rc_mongo))

        results = []
        for name, fn in impls:
            try:
                fn(path)
                results.append((name, None))
            except ValueError as e:
                results.append((name, str(e)))

        errors = [r for _, r in results if r is not None]
        no_errors = [r for _, r in results if r is None]
        if expect_error:
            assert len(no_errors) == 0, (
                f"path={path!r}: expected all to raise, but these did not: {[n for n, r in results if r is None]}"
            )
        else:
            assert len(errors) == 0, (
                f"path={path!r}: expected no raise, but these raised: {[(n, r) for n, r in results if r is not None]}"
            )

    @pytest.mark.parametrize(
        "prefix,path,expected",
        [
            # Exact match
            ("/team/", "/team/", True),
            ("/team", "/team", True),
            ("/", "/", True),
            # Child under directory-style prefix
            ("/team/", "/team/file.txt", True),
            ("/team", "/team/file.txt", True),
            # Root covers everything
            ("/", "/any/path", True),
            ("/", "/a", True),
            # Segment boundary: /work must NOT match /workspace
            ("/work", "/workspace/file.txt", False),
            ("/work/", "/workspace/file.txt", False),
            # Unrelated prefix
            ("/other/", "/team/file.txt", False),
            # Trailing-slash exact match (regression for double-normalization bug)
            ("/team/", "/team/", True),
        ],
    )
    def test_prefix_matches_parity(self, prefix: str, path: str, expected: bool):
        """Both _prefix_matches copies return the same result.

        The mongo copy requires motor; when not installed only the sql copy is checked
        against the expected value.
        """
        import importlib.util

        from vfs.stores.sql_metadata import _prefix_matches as pm_sql

        sql_result = pm_sql(prefix, path)
        assert sql_result == expected, (
            f"sql _prefix_matches({prefix!r}, {path!r}): expected {expected}, got {sql_result}"
        )

        if importlib.util.find_spec("motor") is not None:
            from vfs.stores.mongo_metadata import _prefix_matches as pm_mongo

            mongo_result = pm_mongo(prefix, path)
            assert mongo_result == sql_result, (
                f"_prefix_matches({prefix!r}, {path!r}): sql={sql_result}, mongo={mongo_result}"
            )


# ---------------------------------------------------------------------------
# Issue 2 — Segment-aware permission prefix matching
# ---------------------------------------------------------------------------


class TestSegmentAwarePermissions:
    """PathPrefixPermissions/SegmentBoundaryNotBypassed."""

    @pytest.mark.asyncio
    async def test_prefix_without_slash_does_not_match_longer_sibling(self, sqlite_store):
        """Grant on /work must NOT cover /workspace/file (the old startswith bug)."""
        await sqlite_store.set_permission(_perm("ns", "p1", "/work", {"read"}))
        result = await sqlite_store.check_permission("p1", "ns", "/workspace/file.txt", "read")
        assert result is False

    @pytest.mark.asyncio
    async def test_prefix_without_slash_matches_exact(self, sqlite_store):
        """Grant on /work covers /work exactly (single-file grant)."""
        await sqlite_store.set_permission(_perm("ns", "p1", "/work", {"read"}))
        result = await sqlite_store.check_permission("p1", "ns", "/work", "read")
        assert result is True

    @pytest.mark.asyncio
    async def test_prefix_without_slash_matches_child(self, sqlite_store):
        """Grant on /work covers /work/file.txt."""
        await sqlite_store.set_permission(_perm("ns", "p1", "/work", {"read"}))
        result = await sqlite_store.check_permission("p1", "ns", "/work/file.txt", "read")
        assert result is True

    @pytest.mark.asyncio
    async def test_prefix_with_slash_matches_child(self, sqlite_store):
        """Grant on /workspace/ covers /workspace/file.txt."""
        await sqlite_store.set_permission(_perm("ns", "p1", "/workspace/", {"read"}))
        result = await sqlite_store.check_permission("p1", "ns", "/workspace/file.txt", "read")
        assert result is True

    @pytest.mark.asyncio
    async def test_prefix_with_slash_does_not_match_sibling(self, sqlite_store):
        """Grant on /workspace/ does NOT cover /workspacex/file.txt."""
        await sqlite_store.set_permission(_perm("ns", "p1", "/workspace/", {"read"}))
        result = await sqlite_store.check_permission("p1", "ns", "/workspacex/file.txt", "read")
        assert result is False

    @pytest.mark.asyncio
    async def test_root_grant_covers_everything(self, sqlite_store):
        """Grant on / covers any absolute path."""
        await sqlite_store.set_permission(_perm("ns", "p1", "/", {"read"}))
        assert await sqlite_store.check_permission("p1", "ns", "/any/path/here", "read") is True

    @pytest.mark.asyncio
    async def test_store_check_permission_safe_for_canonical_paths(self, sqlite_store):
        """The store's check_permission is correct for canonical paths.

        The VFS boundary (_require_canonical) rejects non-canonical paths before any
        store call, so the store does NOT apply normpath — doing so would strip trailing
        slashes and break exact-match grants like '/team/'.  A canonical file path
        '/public/secret' must not match a grant on '/other/' — segment boundary is enforced.
        """
        await sqlite_store.set_permission(_perm("ns", "p1", "/other/", {"read"}, pid="p1a"))
        # Canonical path outside the grant prefix must be rejected.
        result = await sqlite_store.check_permission("p1", "ns", "/public/secret", "read")
        assert result is False

    @pytest.mark.asyncio
    async def test_set_permission_rejects_non_canonical_prefix(self, sqlite_store):
        """set_permission must reject a non-canonical path_prefix."""
        with pytest.raises(ValueError):
            await sqlite_store.set_permission(_perm("ns", "p1", "/a/../b", {"read"}))

    @pytest.mark.asyncio
    async def test_trailing_slash_admin_grant_self_check_passes(self, vfs_instance):
        """Regression: non-root admin whose only grant is '/team/' must pass
        check_permission(..., '/team/', 'admin') so VFS.grant() on that prefix succeeds.

        Before the fix, check_permission stripped the trailing slash via normpath
        ('/team/' → '/team'), making the exact-match fail and grant() raise
        PermissionDeniedError even though the principal held the '/team/' admin grant.
        """
        from vfs.errors import PermissionDeniedError

        admin = await vfs_instance.create_principal("root_admin")
        team_admin = await vfs_instance.create_principal("team_admin")
        user = await vfs_instance.create_principal("team_user")
        ns = await vfs_instance.create_namespace("team-ns", admin.id)
        await vfs_instance.bootstrap_admin(admin.id, ns.id)
        # Grant team_admin admin rights on '/team/' only (not root).
        await vfs_instance.grant(admin.id, team_admin.id, ns.id, "/team/", {"admin", "write", "read"})
        # team_admin must be able to grant on '/team/' without PermissionDeniedError.
        try:
            await vfs_instance.grant(team_admin.id, user.id, ns.id, "/team/", {"read"})
        except PermissionDeniedError:
            pytest.fail(
                "team_admin with '/team/' admin grant could not grant on '/team/' "
                "(double-normalization bug: check_permission stripped trailing slash)"
            )


# ---------------------------------------------------------------------------
# Issue 3 — LIKE wildcard escaping
# ---------------------------------------------------------------------------


class TestLikeEscaping:
    """PrefixQueryLiteralMatching: _ and % in paths must not act as wildcards."""

    @pytest.mark.asyncio
    async def test_underscore_not_matched_as_wildcard(self, sqlite_store):
        """Files under /my_dir/ must NOT appear when listing /myXdir/."""
        now = _now()
        await sqlite_store.put_file(
            FileMeta(
                namespace_id="ns",
                path="/my_dir/report.txt",
                current_version_id="v1",
                current_version_number=1,
                created_at=now,
                updated_at=now,
            )
        )
        # List a prefix where _ would match any character as a wildcard.
        results = await sqlite_store.list_dir("ns", "/myXdir/", recursive=True)
        assert results == [], f"Expected empty list, got {[r.path for r in results]}"

        # Regression guard: list with the underscore-containing prefix and verify a sibling
        # path (same length, different char) is NOT returned. Without LIKE escaping,
        # '/my_dir/%' treats '_' as a single-char SQL wildcard and would also match
        # '/myZdir/file.txt', producing a false positive.
        await sqlite_store.put_file(
            FileMeta(
                namespace_id="ns",
                path="/myZdir/file.txt",
                current_version_id="v2",
                current_version_number=1,
                created_at=now,
                updated_at=now,
            )
        )
        results2 = await sqlite_store.list_dir("ns", "/my_dir/", recursive=True)
        paths2 = [r.path for r in results2]
        assert "/myZdir/file.txt" not in paths2, "underscore in prefix acted as SQL wildcard — LIKE escaping is broken"

    @pytest.mark.asyncio
    async def test_underscore_prefix_finds_own_file(self, sqlite_store):
        """Listing /my_dir/ must return files actually under /my_dir/."""
        now = _now()
        await sqlite_store.put_file(
            FileMeta(
                namespace_id="ns",
                path="/my_dir/report.txt",
                current_version_id="v1",
                current_version_number=1,
                created_at=now,
                updated_at=now,
            )
        )
        results = await sqlite_store.list_dir("ns", "/my_dir/", recursive=True)
        paths = [r.path for r in results]
        assert "/my_dir/report.txt" in paths

    @pytest.mark.asyncio
    async def test_percent_in_prefix_matches_literally(self, sqlite_store):
        """A path containing '%' is found when listed by its exact prefix."""
        now = _now()
        await sqlite_store.put_file(
            FileMeta(
                namespace_id="ns",
                path="/data%2F/file.txt",
                current_version_id="v1",
                current_version_number=1,
                created_at=now,
                updated_at=now,
            )
        )
        results = await sqlite_store.list_dir("ns", "/data%2F/", recursive=True)
        paths = [r.path for r in results]
        assert "/data%2F/file.txt" in paths

    @pytest.mark.asyncio
    async def test_percent_does_not_match_arbitrary_paths(self, sqlite_store):
        """Listing /data%2F/ must not return paths that merely satisfy /data??/."""
        now = _now()
        await sqlite_store.put_file(
            FileMeta(
                namespace_id="ns",
                path="/dataXYZ/file.txt",
                current_version_id="v1",
                current_version_number=1,
                created_at=now,
                updated_at=now,
            )
        )
        results = await sqlite_store.list_dir("ns", "/data%2F/", recursive=True)
        assert results == [], f"Expected empty, got {[r.path for r in results]}"

        # Regression guard: a path whose prefix matches only when '%' is treated as a
        # SQL wildcard must NOT be returned. Without LIKE escaping, the pattern
        # '/data%2F/%' would match '/data/2F/extra.txt' because '%' matches any sequence.
        await sqlite_store.put_file(
            FileMeta(
                namespace_id="ns",
                path="/data/2F/extra.txt",
                current_version_id="v2",
                current_version_number=1,
                created_at=now,
                updated_at=now,
            )
        )
        results2 = await sqlite_store.list_dir("ns", "/data%2F/", recursive=True)
        paths2 = [r.path for r in results2]
        assert "/data/2F/extra.txt" not in paths2, "percent in prefix acted as SQL wildcard — LIKE escaping is broken"


# ---------------------------------------------------------------------------
# Issue 4 — Concurrent no-CAS write retry
# ---------------------------------------------------------------------------


class _InjectRaceStore(SQLiteMetadataStore):
    """SQLiteMetadataStore subclass that injects a competing put_version the first time
    get_file is called for a specific path, simulating a version-number race."""

    def __init__(self, db_path: str, *, inject_path: str) -> None:
        super().__init__(db_path)
        self._inject_path = inject_path
        self._injected = False

    async def get_file(self, namespace_id: str, path: str) -> FileMeta | None:
        result = await super().get_file(namespace_id, path)
        if path == self._inject_path and not self._injected and result is not None:
            self._injected = True
            # Insert the next version so the caller will collide on version_number.
            competing = _version(namespace_id, path, result.current_version_number + 1, content_hash="race")
            await super().put_version(competing)
        return result


@pytest_asyncio.fixture
async def racing_store(tmp_path):
    store = _InjectRaceStore(str(tmp_path / "race.db"), inject_path="/a.txt")
    await store.initialize()
    yield store
    await store.close()


@pytest_asyncio.fixture
async def racing_vfs(tmp_path):
    """VFS instance backed by the racing store."""
    from vfs.stores.local_blob import LocalFSBlobStore

    blob_path = str(tmp_path / "blobs")
    config = VFSConfig(
        metadata_store_uri=f"sqlite:///{tmp_path / 'race.db'}",
        blob_store_uri=f"file:///{blob_path}/",
        otel_enabled=False,
        audit_log_enabled=False,
    )
    vfs = VFS(config)
    # Swap the metadata store with the racing version before initialize().
    vfs._meta = _InjectRaceStore(str(tmp_path / "race.db"), inject_path="/a.txt")
    await vfs._meta.initialize()
    yield vfs
    await vfs._meta.close()


class TestVersionCollisionError:
    """NoCASVersionCollision — stores expose VersionCollisionError, VFS retries."""

    @pytest.mark.asyncio
    async def test_store_raises_version_collision_on_duplicate_version_number(self, sqlite_store):
        """BaseSqlMetadataStore.put_version must raise VersionCollisionError (not
        IntegrityError) when two no-CAS writes collide on the same version_number."""
        v1 = _version("ns", "/a.txt", 1)
        await sqlite_store.put_version(v1)

        # Simulate a racing writer: insert a version with the same number using
        # a separate VersionMeta (different id, same version_number).
        from ulid import ULID

        v2 = VersionMeta(
            id=str(ULID()),
            file_path="/a.txt",
            namespace_id="ns",
            version_number=2,
            content_hash="h_racer",
            size=10,
            created_at=_now(),
            created_by="racer",
        )
        await sqlite_store.put_version(v2)

        # Now try to insert another v2 — must get VersionCollisionError.
        from ulid import ULID as _ULID

        duplicate = VersionMeta(
            id=str(_ULID()),
            file_path="/a.txt",
            namespace_id="ns",
            version_number=2,
            content_hash="h_duplicate",
            size=10,
            created_at=_now(),
            created_by="racer2",
        )
        with pytest.raises(VersionCollisionError):
            await sqlite_store.put_version(duplicate)

    @pytest.mark.asyncio
    async def test_cas_conflict_still_raises_conflict_error(self, sqlite_store):
        """expected_version mismatch must still raise ConflictError, not VersionCollisionError."""
        v1 = _version("ns", "/b.txt", 1)
        await sqlite_store.put_version(v1)
        v2 = _version("ns", "/b.txt", 2)
        with pytest.raises(ConflictError):
            await sqlite_store.put_version(v2, expected_version=99)

    @pytest.mark.asyncio
    async def test_vfs_write_retries_on_injected_race(self, racing_vfs):
        """VFS.write retries when the store signals VersionCollisionError, and the
        write ultimately lands at version N+2 after the race at N+1."""
        admin = await racing_vfs.create_principal("admin")
        ns = await racing_vfs.create_namespace("ns-race", admin.id)
        await racing_vfs.bootstrap_admin(admin.id, ns.id)
        await racing_vfs.grant(admin.id, admin.id, ns.id, "/", {"write"})

        # First write: creates version 1.
        v1 = await racing_vfs.write(ns.id, "/a.txt", b"v1", principal_id=admin.id)
        assert v1.version_number == 1

        # Second write: racing_store will inject a competing version 2 between
        # get_file and put_version, forcing a VersionCollisionError on the first
        # attempt. The retry reads version 2 (from the injected racer) and
        # successfully writes version 3.
        async with no_task_leaks(action="raise"):
            v_retry = await racing_vfs.write(ns.id, "/a.txt", b"v_retry", principal_id=admin.id)
        # The injected racer took version 2; VFS retry lands at 3.
        assert v_retry.version_number == 3

    @pytest.mark.asyncio
    async def test_vfs_write_cas_conflict_not_retried(self, vfs_instance):
        """ConflictError from expected_version CAS must propagate immediately."""
        admin = await vfs_instance.create_principal("admin3")
        ns = await vfs_instance.create_namespace("ns-cas", admin.id)
        await vfs_instance.bootstrap_admin(admin.id, ns.id)
        await vfs_instance.grant(admin.id, admin.id, ns.id, "/", {"write"})

        await vfs_instance.write(ns.id, "/c.txt", b"v1", principal_id=admin.id)
        await vfs_instance.write(ns.id, "/c.txt", b"v2", principal_id=admin.id)

        with pytest.raises(ConflictError):
            await vfs_instance.write(ns.id, "/c.txt", b"stale", principal_id=admin.id, expected_version=1)

    @pytest.mark.asyncio
    async def test_concurrent_no_cas_writes_both_succeed(self, vfs_instance):
        """Two concurrent no-CAS writes on the same file both succeed with
        distinct version numbers (last-writer-wins semantics with retry)."""
        admin = await vfs_instance.create_principal("admin4")
        ns = await vfs_instance.create_namespace("ns-concurrent", admin.id)
        await vfs_instance.bootstrap_admin(admin.id, ns.id)
        await vfs_instance.grant(admin.id, admin.id, ns.id, "/", {"write"})

        # Seed the file.
        await vfs_instance.write(ns.id, "/d.txt", b"seed", principal_id=admin.id)

        async with no_task_leaks(action="raise"):
            results = await asyncio.gather(
                vfs_instance.write(ns.id, "/d.txt", b"writer_a", principal_id=admin.id),
                vfs_instance.write(ns.id, "/d.txt", b"writer_b", principal_id=admin.id),
            )

        version_numbers = {r.version_number for r in results}
        seed_version = 1
        assert len(version_numbers) == 2, f"Expected two distinct version numbers, got {version_numbers}"
        assert all(v > seed_version for v in version_numbers), (
            f"Both results must have version numbers > seed ({seed_version}), got {version_numbers}"
        )


# ---------------------------------------------------------------------------
# Issue 3 (move) — retry bugs: stale src_file and dst double-insert
# ---------------------------------------------------------------------------


class _InjectMoveSrcCollisionStore(SQLiteMetadataStore):
    """Injects a competing tombstone at src on the first move() attempt, forcing
    a VersionCollisionError on the src tombstone write.  The retry must re-read
    src_file to get a fresh version_number (bug 1 fix verification)."""

    def __init__(self, db_path: str, *, src_path: str) -> None:
        super().__init__(db_path)
        self._src_path = src_path
        self._injected = False

    async def get_file(self, namespace_id: str, path: str):
        result = await super().get_file(namespace_id, path)
        # Inject a competing tombstone at src on the first get_file call for src
        # that returns a non-None file (i.e., src exists and the move is about to start).
        if path == self._src_path and not self._injected and result is not None:
            self._injected = True
            from ulid import ULID

            competing_tombstone = VersionMeta(
                id=str(ULID()),
                file_path=self._src_path,
                namespace_id=namespace_id,
                version_number=result.current_version_number + 1,
                content_hash="",
                size=0,
                created_at=_now(),
                created_by="racer",
                is_tombstone=True,
            )
            # Insert directly via the parent to bypass our hook.
            await super().put_version(competing_tombstone)
        return result


class _InjectMoveDstCollisionStore(SQLiteMetadataStore):
    """Injects a competing version at dst on the first move() attempt, forcing
    a VersionCollisionError on the dst write.  The retry must correctly re-read
    dst_file and advance the version_number (bug 2 / general dst collision)."""

    def __init__(self, db_path: str, *, dst_path: str) -> None:
        super().__init__(db_path)
        self._dst_path = dst_path
        self._injected = False

    async def get_file(self, namespace_id: str, path: str):
        await super().get_file(namespace_id, path)
        # Inject on the first get_file call for dst when the file doesn't yet exist
        # (version_number 1 is about to be written) so the dst write will collide.
        if path == self._dst_path and not self._injected:
            self._injected = True
            from ulid import ULID

            # A racing writer already wrote dst at version 1.
            competing = VersionMeta(
                id=str(ULID()),
                file_path=self._dst_path,
                namespace_id=namespace_id,
                version_number=1,
                content_hash="race_content_hash_xxxxxxxxxx",
                size=5,
                created_at=_now(),
                created_by="racer",
            )
            await super().put_version(competing)
        return await super().get_file(namespace_id, path)


class TestMoveRetryBugs:
    """MoveRetryBugs: move() re-reads src_file inside the retry loop and
    handles dst-side collision without duplicating the destination."""

    @pytest.mark.asyncio
    async def test_move_retries_on_src_tombstone_collision(self, tmp_path):
        """SrcTombstoneCollision: when a competing writer takes the src tombstone
        version_number, move() re-reads src_file and uses the fresh version_number
        so the retry succeeds rather than re-colliding on the same stale number."""
        from vfs.stores.local_blob import LocalFSBlobStore

        store = _InjectMoveSrcCollisionStore(str(tmp_path / "move_src.db"), src_path="/src.txt")
        await store.initialize()
        blob = LocalFSBlobStore(str(tmp_path / "blobs"))
        config = VFSConfig(
            metadata_store_uri=f"sqlite:///{tmp_path / 'move_src.db'}",
            blob_store_uri=f"file:///{tmp_path / 'blobs'}/",
            otel_enabled=False,
            audit_log_enabled=False,
        )
        vfs = VFS(config)
        vfs._meta = store
        vfs._blob = blob

        admin = await vfs.create_principal("move_src_admin")
        ns = await vfs.create_namespace("ns-move-src", admin.id)
        await vfs.bootstrap_admin(admin.id, ns.id)
        await vfs.grant(admin.id, admin.id, ns.id, "/", {"read", "write", "delete"})

        await vfs.write(ns.id, "/src.txt", b"content", principal_id=admin.id)
        # move() should retry past the injected src tombstone collision.
        result = await vfs.move(ns.id, "/src.txt", "/dst.txt", principal_id=admin.id)
        assert result.file_path == "/dst.txt"
        # src must be tombstoned.
        src_ver = await store.get_version(ns.id, "/src.txt")
        assert src_ver is not None and src_ver.is_tombstone
        await store.close()

    @pytest.mark.asyncio
    async def test_move_retries_on_dst_version_collision(self, tmp_path):
        """DstVersionCollision: when a racing writer takes dst version 1,
        move() re-reads dst_file and succeeds at version 2."""
        from vfs.stores.local_blob import LocalFSBlobStore

        store = _InjectMoveDstCollisionStore(str(tmp_path / "move_dst.db"), dst_path="/dst.txt")
        await store.initialize()
        blob = LocalFSBlobStore(str(tmp_path / "blobs"))
        config = VFSConfig(
            metadata_store_uri=f"sqlite:///{tmp_path / 'move_dst.db'}",
            blob_store_uri=f"file:///{tmp_path / 'blobs'}/",
            otel_enabled=False,
            audit_log_enabled=False,
        )
        vfs = VFS(config)
        vfs._meta = store
        vfs._blob = blob

        admin = await vfs.create_principal("move_dst_admin")
        ns = await vfs.create_namespace("ns-move-dst", admin.id)
        await vfs.bootstrap_admin(admin.id, ns.id)
        await vfs.grant(admin.id, admin.id, ns.id, "/", {"read", "write", "delete"})

        await vfs.write(ns.id, "/src.txt", b"content", principal_id=admin.id)
        # move() must retry past the injected dst collision and land at dst version 2.
        result = await vfs.move(ns.id, "/src.txt", "/dst.txt", principal_id=admin.id)
        assert result.file_path == "/dst.txt"
        assert result.version_number == 2  # racer took v1, we land at v2
        await store.close()
