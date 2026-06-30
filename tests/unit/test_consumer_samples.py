"""Tests for the consumer samples in ``notebooks/``.

Two independent concerns, neither of which imports the demo notebook (the
notebook is documentation-that-runs, validated by executing the real ``#%%`` file
through a kernel — not by importing its cells):

1. **Public-surface invariant** — both notebook samples import only names from the
   top-level ``vfs`` package; reaching into a ``vfs.*`` submodule is a coupling
   violation. Static AST check over the source, so it runs without any optional dep.
2. **Consumer integration** — the pattern the pydantic-ai sample teaches (drive a
   sandbox execute, then a native write through the governed mount via the public
   ``vfs`` API) works end-to-end against an in-memory VFS.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

from pyleak import no_task_leaks
import pytest
import pytest_asyncio

import vfs as vfs_pkg
from vfs import VFS, ResourceLimits, VFSConfig

HAS_MONTY = importlib.util.find_spec("pydantic_monty") is not None

NOTEBOOKS = Path(__file__).resolve().parents[2] / "notebooks"
PUBLIC_VFS_NAMES = set(vfs_pkg.__all__)
SAMPLE_FILES = ["03_pydantic_ai_codemode.py", "langgraph_smoke.py"]


def _vfs_imports(source: str) -> tuple[list[str], list[str]]:
    """Return ``(internal_modules, names_from_vfs)`` for the ``vfs`` imports in ``source``.

    ``internal_modules`` are ``vfs.<submodule>`` reach-throughs (a coupling
    violation); ``names_from_vfs`` are the names pulled via ``from vfs import …``.
    """
    tree = ast.parse(source)
    internal: list[str] = []
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "vfs":
                names.extend(alias.name for alias in node.names)
            elif node.module and node.module.startswith("vfs."):
                internal.append(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("vfs."):
                    internal.append(alias.name)
    return internal, names


class TestSamplesUsePublicSurfaceOnly:
    """Both samples import only public ``vfs`` names — no internal reach-through."""

    @pytest.mark.parametrize("filename", SAMPLE_FILES)
    def test_no_internal_vfs_imports(self, filename):
        source = (NOTEBOOKS / filename).read_text()
        internal, names = _vfs_imports(source)
        assert not internal, f"{filename} reaches into vfs internals: {internal}"
        non_public = [n for n in names if n not in PUBLIC_VFS_NAMES]
        assert not non_public, f"{filename} imports non-public vfs names: {non_public}"


@pytest_asyncio.fixture
async def env(tmp_path):
    """In-memory VFS + an agent principal with full rights on ``/``.

    Returns ``(vfs, namespace_id, principal_id)``.
    """
    config = VFSConfig(
        metadata_store_uri=f"sqlite:///{tmp_path}/demo.db",
        blob_store_uri=f"file:///{tmp_path}/blobs/",
        otel_enabled=False,
        audit_log_enabled=False,
        blob_cache_enabled=False,
    )
    vfs = VFS(config)
    await vfs.initialize()
    namespace = await vfs.create_namespace("codemode", "system")
    admin = await vfs.create_principal("admin")
    await vfs.bootstrap_admin(admin.id, namespace.id)
    agent = await vfs.create_principal("agent")
    await vfs.grant(admin.id, agent.id, namespace.id, "/", {"read", "write", "delete", "execute"})
    yield vfs, namespace.id, agent.id
    await vfs.close()


@pytest.mark.skipif(not HAS_MONTY, reason="pydantic-monty not installed")
class TestConsumerCodeModePattern:
    """The public-surface consumer pattern: one sandbox execute + one native write."""

    @pytest.mark.asyncio
    async def test_execute_and_native_write(self, env):
        vfs, namespace_id, principal_id = env

        async with no_task_leaks(action="raise"):
            # One sandbox execute through the governed VFS.
            result = await vfs.execute(
                code="6 * 7",
                namespace_id=namespace_id,
                principal_id=principal_id,
                provider_name="monty",
                resource_limits=ResourceLimits(timeout_seconds=10.0),
            )
            assert result.success and result.output == 42

            # A native write through the governed mount: the sandbox uses plain
            # open().write(), and the bytes land as a new governed version.
            await vfs.write(
                namespace_id=namespace_id,
                path="/greeting.txt",
                content=b"hello\nworld\n",
                principal_id=principal_id,
            )
            wrote = await vfs.execute(
                code="open('/greeting.txt', 'w').write('HELLO\\nworld\\n')",
                namespace_id=namespace_id,
                principal_id=principal_id,
                provider_name="monty",
                resource_limits=ResourceLimits(timeout_seconds=10.0),
            )
            assert wrote.success

            content = await vfs.read(namespace_id=namespace_id, path="/greeting.txt", principal_id=principal_id)
            assert content == b"HELLO\nworld\n"
