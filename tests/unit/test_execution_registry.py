"""Unit tests for the execution provider registry (``resolve_execution_provider``).

Covers the ``ExecutionProviderRegistry`` requirement scenarios (tasks.md group
"execution — registry & extras"):
  ExecutionProviderRegistry/UnknownProviderRejected
  ExecutionProviderRegistry/MissingMontyExtraRaises
  ExecutionProviderRegistry/MissingJustBashExtraRaises
  ExecutionProviderRegistry/VfsImportsWithoutAnyProvider

The missing-extra scenarios simulate the absence of an optional dependency by
monkeypatching ``importlib.util.find_spec`` (the lazy-import guard the registry
uses), so they run regardless of which extras are installed in the test env.
"""

from __future__ import annotations

import importlib.util

import pytest
import pytest_asyncio

from vfs.config import VFSConfig
from vfs.execution.registry import resolve_execution_provider
from vfs.vfs import VFS

# Driver guard modules the registry probes via ``importlib.util.find_spec``.
_MONTY_DRIVER = "pydantic_monty"
_JUST_BASH_DRIVER = "just_bash"


def _find_spec_absent(*absent: str):
    """Return a ``find_spec`` replacement that reports ``absent`` modules missing.

    All other modules resolve through the real ``importlib.util.find_spec`` so
    the rest of the interpreter is unaffected.
    """
    real_find_spec = importlib.util.find_spec

    def fake(name: str, package: str | None = None):
        if name in absent:
            return None
        return real_find_spec(name, package)

    return fake


# ---------------------------------------------------------------------------
# ExecutionProviderRegistry/UnknownProviderRejected
# ---------------------------------------------------------------------------


class TestUnknownProviderRejected:
    """An unknown provider name raises before any session/FsOperations is built."""

    def test_unknown_provider_raises_value_error(self):
        with pytest.raises(ValueError, match="nonexistent") as exc_info:
            resolve_execution_provider("nonexistent", VFSConfig())
        # The message names the unknown provider and lists the known ones.
        assert "Unknown execution provider" in str(exc_info.value)


# ---------------------------------------------------------------------------
# ExecutionProviderRegistry/MissingMontyExtraRaises
# ---------------------------------------------------------------------------


class TestMissingMontyExtraRaises:
    """A missing ``monty`` extra yields an actionable install message, not a traceback."""

    def test_missing_monty_extra(self, monkeypatch):
        monkeypatch.setattr(importlib.util, "find_spec", _find_spec_absent(_MONTY_DRIVER))
        with pytest.raises(ImportError) as exc_info:
            resolve_execution_provider("monty", VFSConfig())
        msg = str(exc_info.value)
        assert "ai-vfs[monty]" in msg
        assert "pip install" in msg


# ---------------------------------------------------------------------------
# ExecutionProviderRegistry/MissingJustBashExtraRaises
# ---------------------------------------------------------------------------


class TestMissingJustBashExtraRaises:
    """A missing ``just-bash`` extra yields an actionable install message, not a traceback."""

    def test_missing_just_bash_extra(self, monkeypatch):
        monkeypatch.setattr(importlib.util, "find_spec", _find_spec_absent(_JUST_BASH_DRIVER))
        with pytest.raises(ImportError) as exc_info:
            resolve_execution_provider("just-bash", VFSConfig())
        msg = str(exc_info.value)
        assert "ai-vfs[just-bash]" in msg
        assert "pip install" in msg


# ---------------------------------------------------------------------------
# ExecutionProviderRegistry/VfsImportsWithoutAnyProvider
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def vfs_inst(tmp_path):
    """Lightweight in-process VFS backed by SQLite + local FS blob."""
    db_path = str(tmp_path / "test.db")
    blob_path = str(tmp_path / "blobs")
    config = VFSConfig(
        metadata_store_uri=f"sqlite:///{db_path}",
        blob_store_uri=f"file:///{blob_path}/",
        otel_enabled=False,
        audit_log_enabled=False,
        blob_cache_enabled=False,
    )
    v = VFS(config)
    await v.initialize()
    yield v
    await v.close()


class TestVfsImportsWithoutAnyProvider:
    """A non-execute VFS op succeeds with neither execution extra importable.

    Providers are resolved only inside ``vfs.execute``; an ordinary write/read
    never touches the registry, so the VFS layer is unaffected by their absence.
    """

    @pytest.mark.asyncio
    async def test_non_execute_op_succeeds_without_providers(self, vfs_inst, monkeypatch):
        # Make both execution drivers appear absent for the duration of the op.
        monkeypatch.setattr(importlib.util, "find_spec", _find_spec_absent(_MONTY_DRIVER, _JUST_BASH_DRIVER))

        ns = await vfs_inst.create_namespace("reg-ns", "admin")
        admin = await vfs_inst.create_principal("admin")
        await vfs_inst.bootstrap_admin(admin.id, ns.id)
        agent = await vfs_inst.create_principal("agent")
        await vfs_inst.grant(admin.id, agent.id, ns.id, "/", {"read", "write"})

        await vfs_inst.write(ns.id, "/doc.txt", b"hello", principal_id=agent.id)
        content = await vfs_inst.read(ns.id, "/doc.txt", principal_id=agent.id)
        assert content == b"hello"
