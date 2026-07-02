"""Unit tests for ExecutionProvider protocol, models, and error types.

Covers:
  ExecutionProtocol/ExecutionResultFields
  ExecutionProtocol/ExecutionResultFailureFields
  ExecutionProtocol/ResourceLimitsDefaults
"""

from __future__ import annotations

import pytest

from vfs.errors import OperationBudgetExceededError, VFSError
from vfs.protocols.execution import ExecutionCapabilities, ExecutionResult, ResourceLimits


class TestExecutionResult:
    """ExecutionProtocol/ExecutionResultFields and ExecutionResultFailureFields."""

    def test_success_fields(self):
        """ExecutionResultFields: success=True with output; optional fields default to None."""
        result = ExecutionResult(success=True, output=42)
        assert result.success is True
        assert result.output == 42
        assert result.error_type is None
        assert result.error_message is None

    def test_failure_fields(self):
        """ExecutionResultFailureFields: all failure fields populated."""
        result = ExecutionResult(
            success=False,
            error_type="conflict",
            error_message="Version conflict; re-read and retry",
        )
        assert result.success is False
        assert result.error_type == "conflict"
        assert result.error_message
        assert result.output is None

    def test_frozen(self):
        """ExecutionResult is frozen: mutation raises."""
        result = ExecutionResult(success=True, output=1)
        with pytest.raises((AttributeError, TypeError)):
            result.success = False  # type: ignore[misc]

    def test_output_any_type(self):
        """output field accepts any Python value."""
        assert ExecutionResult(success=True, output={"key": [1, 2, 3]}).output == {"key": [1, 2, 3]}
        assert ExecutionResult(success=True, output=None).output is None


class TestExecutionCapabilities:
    """ExecutionCapabilities is a frozen dataclass."""

    def test_fields(self):
        caps = ExecutionCapabilities(supports_async=True, language="python", tier="monty")
        assert caps.supports_async is True
        assert caps.language == "python"
        assert caps.tier == "monty"

    def test_enforces_memory_limit_defaults_false_and_is_queryable(self):
        """Callers can feature-detect which provider honours max_memory_bytes."""
        assert (
            ExecutionCapabilities(supports_async=True, language="bash", tier="just-bash").enforces_memory_limit
            is False
        )
        monty = ExecutionCapabilities(supports_async=True, language="python", tier="monty", enforces_memory_limit=True)
        assert monty.enforces_memory_limit is True

    def test_frozen(self):
        caps = ExecutionCapabilities(supports_async=False, language="python", tier="test")
        with pytest.raises((AttributeError, TypeError)):
            caps.language = "ruby"  # type: ignore[misc]


class TestResourceLimitsDefaults:
    """ExecutionProtocol/ResourceLimitsDefaults: defaults match §8.3."""

    def test_defaults(self):
        """ResourceLimitsDefaults: timeout_seconds=30.0, max_operations=1000."""
        limits = ResourceLimits()
        assert limits.timeout_seconds == 30.0
        assert limits.max_operations == 1000
        assert limits.max_memory_bytes is None
        assert limits.max_read_bytes is None
        assert limits.max_result_items is None

    def test_override(self):
        limits = ResourceLimits(timeout_seconds=5.0, max_operations=50, max_read_bytes=1024)
        assert limits.timeout_seconds == 5.0
        assert limits.max_operations == 50
        assert limits.max_read_bytes == 1024


class TestNewErrors:
    """OperationBudgetExceededError is a VFSError subclass."""

    def test_operation_budget_exceeded_is_vfs_error(self):
        err = OperationBudgetExceededError("budget gone")
        assert isinstance(err, VFSError)
        assert "budget gone" in str(err)
