"""Ephemeral VFS lifecycle for the demo — built on a temp dir, torn down on exit.

The point: your real files are never exposed. The
store is a throwaway SQLite + local-blob VFS under a temp directory, seeded with
this repo's own contract specs so the agent has real material to explore and edit.
Nothing persists across restarts — this is a "works on my machine" demo, not a
deployable service.
"""

from __future__ import annotations

from dataclasses import dataclass
import pathlib
import shutil
import tempfile

from vfs import VFS, Session, VFSConfig


@dataclass
class DemoWorld:
    """The live objects the server shares across every request and inspector call."""

    vfs: VFS
    namespace_id: str
    admin_id: str  # full-visibility principal used by the read-only inspector
    agent_id: str  # constrained principal the LLM acts as
    tmp_dir: str


async def build_world(repo_root: pathlib.Path) -> DemoWorld:
    """Construct the ephemeral VFS, provision principals, and seed the repo's specs."""
    tmp_dir = tempfile.mkdtemp(prefix="vfs-demo-")
    config = VFSConfig(
        metadata_store_uri=f"sqlite:///{tmp_dir}/demo.db",
        blob_store_uri=f"file:///{tmp_dir}/blobs/",
        otel_enabled=False,
        audit_log_enabled=False,
        blob_cache_enabled=False,
    )
    vfs = VFS(config)
    await vfs.initialize()

    ns = await vfs.create_namespace("demo", "system")
    admin = await vfs.create_principal("admin", principal_type="user")
    await vfs.bootstrap_admin(admin.id, ns.id)
    await vfs.grant(admin.id, admin.id, ns.id, "/", {"admin", "read", "write", "delete", "execute"})

    agent_principal = await vfs.create_principal("agent", principal_type="agent")
    await vfs.grant(admin.id, agent_principal.id, ns.id, "/", {"read", "write", "delete", "execute"})

    await _seed_specs(vfs, ns.id, admin.id, repo_root)

    return DemoWorld(vfs=vfs, namespace_id=ns.id, admin_id=admin.id, agent_id=agent_principal.id, tmp_dir=tmp_dir)


async def _seed_specs(vfs: VFS, ns_id: str, principal_id: str, repo_root: pathlib.Path) -> list[str]:
    """Write the baseline specs into the VFS; return the VFS paths created."""
    session = Session(vfs, ns_id, principal_id)
    sources: list[tuple[pathlib.Path, str]] = [(repo_root / ".specs" / "NORTH-STAR.md", "/NORTH-STAR.md")]
    for spec in sorted((repo_root / ".specs" / "specs").glob("*/spec.md")):
        sources.append((spec, f"/specs/{spec.parent.name}/spec.md"))

    written: list[str] = []
    for src, vfs_path in sources:
        if not src.is_file():
            continue
        await session.write(vfs_path, src.read_bytes())
        written.append(vfs_path)
    return written


async def teardown_world(world: DemoWorld) -> None:
    """Close the VFS and delete the temp store — the counterpart to `build_world`."""
    await world.vfs.close()
    shutil.rmtree(world.tmp_dir, ignore_errors=True)
