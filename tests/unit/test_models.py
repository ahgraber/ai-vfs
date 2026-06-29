"""Tests for domain models and exceptions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


class TestExceptions:
    """VFS error hierarchy."""

    def test_vfs_error_is_exception(self):
        from vfs.errors import VFSError

        assert issubclass(VFSError, Exception)

    def test_conflict_error_is_vfs_error(self):
        from vfs.errors import ConflictError, VFSError

        assert issubclass(ConflictError, VFSError)

    def test_permission_denied_error_is_vfs_error(self):
        from vfs.errors import PermissionDeniedError, VFSError

        assert issubclass(PermissionDeniedError, VFSError)

    def test_not_found_error_is_vfs_error(self):
        from vfs.errors import NotFoundError, VFSError

        assert issubclass(NotFoundError, VFSError)

    def test_errors_carry_message(self):
        from vfs.errors import ConflictError, NotFoundError, PermissionDeniedError

        for cls in (ConflictError, NotFoundError, PermissionDeniedError):
            err = cls("test message")
            assert str(err) == "test message"


class TestSearchType:
    """SearchType enum values."""

    def test_search_type_members(self):
        from vfs.models import SearchType

        assert set(SearchType) == {
            SearchType.GLOB,
            SearchType.FIND,
            SearchType.REGEX,
            SearchType.FULLTEXT,
            SearchType.SEMANTIC,
        }


class TestRetentionPolicy:
    """RetentionTier and RetentionPolicy defaults."""

    def test_retention_tier_fields(self):
        from vfs.models import RetentionTier

        tier = RetentionTier(max_age=timedelta(hours=24), keep_every=timedelta(hours=1))
        assert tier.max_age == timedelta(hours=24)
        assert tier.keep_every == timedelta(hours=1)

    def test_retention_tier_keep_every_optional(self):
        from vfs.models import RetentionTier

        tier = RetentionTier(max_age=timedelta(days=365), keep_every=None)
        assert tier.keep_every is None

    def test_retention_policy_defaults(self):
        from datetime import timedelta

        from vfs.models import RetentionPolicy

        policy = RetentionPolicy()
        assert policy.max_recent_versions == 50
        assert policy.keep_first_version is True
        assert policy.keep_current_version is True
        assert len(policy.tiers) == 4
        # Tier 1: last 24 h — keep all versions
        assert policy.tiers[0].max_age == timedelta(hours=24)
        assert policy.tiers[0].keep_every is None
        # Tier 2: last 7 d — keep one per hour
        assert policy.tiers[1].max_age == timedelta(days=7)
        assert policy.tiers[1].keep_every == timedelta(hours=1)
        # Tier 3: last 30 d — keep one per day
        assert policy.tiers[2].max_age == timedelta(days=30)
        assert policy.tiers[2].keep_every == timedelta(days=1)
        # Tier 4: beyond 30 d — keep one per week
        assert policy.tiers[3].keep_every == timedelta(weeks=1)


class TestFileMeta:
    """FileMeta construction and field types."""

    def test_construction(self):
        from vfs.models import FileMeta

        now = datetime.now(timezone.utc)
        meta = FileMeta(
            namespace_id="01JQXYZ",
            path="/src/main.py",
            current_version_id="01JQXYZ_V1",
            current_version_number=1,
            created_at=now,
            updated_at=now,
        )
        assert meta.namespace_id == "01JQXYZ"
        assert meta.path == "/src/main.py"
        assert meta.is_deleted is False

    def test_is_deleted_default_false(self):
        from vfs.models import FileMeta

        now = datetime.now(timezone.utc)
        meta = FileMeta(
            namespace_id="ns",
            path="/f",
            current_version_id="v",
            current_version_number=1,
            created_at=now,
            updated_at=now,
        )
        assert meta.is_deleted is False


class TestVersionMeta:
    """VersionMeta construction."""

    def test_construction(self):
        from vfs.models import VersionMeta

        now = datetime.now(timezone.utc)
        ver = VersionMeta(
            id="01JQXYZ_V1",
            file_path="/src/main.py",
            namespace_id="01JQXYZ",
            version_number=1,
            content_hash="abc123",
            size=42,
            created_at=now,
            created_by="principal1",
        )
        assert ver.version_number == 1
        assert ver.is_tombstone is False
        assert ver.search_meta == {}
        assert ver.parent_version_id is None


class TestPermission:
    """Permission construction."""

    def test_construction(self):
        from vfs.models import Permission

        now = datetime.now(timezone.utc)
        perm = Permission(
            id="01PERM",
            principal_id="principal1",
            namespace_id="ns1",
            path_prefix="/",
            operations={"read", "write"},
            created_at=now,
        )
        assert perm.operations == {"read", "write"}


class TestAuditEvent:
    """AuditEvent construction."""

    def test_construction(self):
        from vfs.models import AuditEvent

        now = datetime.now(timezone.utc)
        event = AuditEvent(
            event_id="01EVT",
            timestamp=now,
            namespace_id="ns1",
            principal_id="p1",
            operation="write",
        )
        assert event.path is None
        assert event.version_id is None
        assert event.detail == {}
        assert event.trace_id is None


class TestSearchResult:
    """SearchResult construction."""

    def test_construction(self):
        from vfs.models import SearchResult

        result = SearchResult(path="/src/main.py")
        assert result.line_number is None
        assert result.match_context is None
        assert result.score == 1.0


class TestNamespace:
    """Namespace construction."""

    def test_construction(self):
        from vfs.models import Namespace

        now = datetime.now(timezone.utc)
        ns = Namespace(
            id="01NS",
            display_name="my-workspace",
            created_at=now,
            created_by="admin",
        )
        assert ns.retention_policy is None


class TestPrincipal:
    """Principal construction."""

    def test_construction(self):
        from vfs.models import Principal

        now = datetime.now(timezone.utc)
        p = Principal(
            id="uuid4-val",
            display_name="agent-bob",
            principal_type="agent",
            created_at=now,
        )
        assert p.principal_type == "agent"


class TestPublicContractSurface:
    """Fix 4: all names callers must catch/use are importable directly from ``vfs``."""

    def test_error_types_importable_from_vfs(self):
        import vfs

        names = [
            "AnchorConflictError",
            "ConflictError",
            "IndexUnavailableError",
            "NotFoundError",
            "OperationBudgetExceededError",
            "PermissionDeniedError",
            "ReadBudgetExceededError",
            "ReindexRequiredError",
            "SearchTypeUnsupportedError",
            "VFSError",
            "VersionCollisionError",
        ]
        for name in names:
            assert hasattr(vfs, name), f"vfs.{name} is not importable"

    def test_execution_types_importable_from_vfs(self):
        import vfs

        names = ["AnchoredEditor", "Hunk", "resolve_execution_provider"]
        for name in names:
            assert hasattr(vfs, name), f"vfs.{name} is not importable"

    def test_names_in_all(self):
        import vfs

        for name in [
            "AnchoredEditor",
            "Hunk",
            "resolve_execution_provider",
            "AnchorConflictError",
            "VersionCollisionError",
        ]:
            assert name in vfs.__all__, f"{name!r} missing from vfs.__all__"


class TestFullTextMatchMode:
    """FulltextMatchMode — enum definition and round-trip through SearchRequest."""

    def test_members_importable_from_models(self):
        """FullTextMatchMode.ALL/ANY are importable from vfs.models and equal themselves."""
        from vfs.models import FullTextMatchMode

        assert FullTextMatchMode.ALL == FullTextMatchMode.ALL
        assert FullTextMatchMode.ANY == FullTextMatchMode.ANY
        assert FullTextMatchMode.ALL != FullTextMatchMode.ANY
        assert FullTextMatchMode.ALL.value == "all"
        assert FullTextMatchMode.ANY.value == "any"

    def _make_request(self, **kwargs):
        from vfs.models import SearchType
        from vfs.protocols.search import SearchRequest
        from vfs.search.reader import ContentReader

        reader = ContentReader(entries=[], blob=None, max_reads=0)
        return SearchRequest(
            query="hello s3",
            scope="/",
            search_type=SearchType.FULLTEXT,
            search_metas=[],
            read_content=reader,
            **kwargs,
        )

    def test_match_mode_round_trips_as_enum(self):
        """A FullTextMatchMode round-trips through SearchRequest.match_mode as the enum type."""
        from vfs.models import FullTextMatchMode

        req = self._make_request(match_mode=FullTextMatchMode.ANY)
        assert req.match_mode is FullTextMatchMode.ANY
        assert isinstance(req.match_mode, FullTextMatchMode)
        assert req.match_mode != "any"  # the type is the enum, not a string

    def test_default_match_mode_is_all(self):
        """FulltextMatchModeDefaultIsAll: SearchRequest without match_mode defaults to ALL."""
        from vfs.models import FullTextMatchMode

        req = self._make_request()
        assert req.match_mode == FullTextMatchMode.ALL

    def test_explicit_any_match_mode(self):
        """SearchRequest with match_mode=ANY yields ANY."""
        from vfs.models import FullTextMatchMode

        req = self._make_request(match_mode=FullTextMatchMode.ANY)
        assert req.match_mode == FullTextMatchMode.ANY
