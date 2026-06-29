# %% [markdown]
# # ai-vfs — LangGraph mount + edit gut-check (DISPOSABLE SKETCH)
#
# **This file is a disposable falsification sketch, not a shipped integration.**
# Its only job is to answer one question: *does the same public `vfs` surface
# the pydantic-ai sample uses also plug into a LangGraph node without reaching
# for library internals?* If this ever needs `vfs.*` submodule imports to work,
# that is a coupling smell to fix in the library — not here.
#
# It imports only the public `vfs` package surface. It is **not** part of the
# test suite and `langgraph` is not a project dependency; run it ad hoc only if
# you have `langgraph` installed (`uv run --with langgraph python
# notebooks/langgraph_smoke.py`).

# %%
from __future__ import annotations

import asyncio
import tempfile
from typing import TypedDict

from langgraph.graph import END, StateGraph

from vfs import VFS, AnchoredEditor, Hunk, ResourceLimits, Session, VFSConfig


class MountState(TypedDict):
    """Graph state threaded between nodes."""

    namespace_id: str
    principal_id: str
    execute_output: str
    new_version: int


async def _make_vfs(tmp_dir: str) -> tuple[VFS, str, str]:
    config = VFSConfig(
        metadata_store_uri=f"sqlite:///{tmp_dir}/lg.db",
        blob_store_uri=f"file:///{tmp_dir}/blobs/",
        otel_enabled=False,
        audit_log_enabled=False,
        blob_cache_enabled=False,
    )
    vfs = VFS(config)
    await vfs.initialize()
    namespace = await vfs.create_namespace("langgraph", "system")
    admin = await vfs.create_principal("admin")
    await vfs.bootstrap_admin(admin.id, namespace.id)
    agent = await vfs.create_principal("agent")
    await vfs.grant(admin.id, agent.id, namespace.id, "/", {"read", "write", "execute"})
    return vfs, namespace.id, agent.id


async def main() -> None:
    """Run a two-node graph: mount-and-execute, then anchored-edit."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        vfs, namespace_id, principal_id = await _make_vfs(tmp_dir)
        try:
            await vfs.write(namespace_id, "/note.txt", b"draft\n", principal_id=principal_id)

            async def mount_and_execute(state: MountState) -> MountState:
                result = await vfs.execute(
                    "21 * 2",
                    state["namespace_id"],
                    state["principal_id"],
                    "monty",
                    resource_limits=ResourceLimits(timeout_seconds=10.0),
                    cwd="/",
                )
                return {**state, "execute_output": repr(result.output)}

            async def anchored_edit(state: MountState) -> MountState:
                editor = AnchoredEditor(Session(vfs, state["namespace_id"], state["principal_id"]))
                read = await editor.read_anchored("/note.txt")
                anchor = read.anchors[0]
                result = await editor.edit_anchored(
                    "/note.txt",
                    [Hunk(start_anchor=anchor, end_anchor=anchor, replacement=["final"])],
                    expected_version=read.version,
                )
                return {**state, "new_version": result.new_version}

            graph = StateGraph(MountState)
            graph.add_node("mount_and_execute", mount_and_execute)
            graph.add_node("anchored_edit", anchored_edit)
            graph.set_entry_point("mount_and_execute")
            graph.add_edge("mount_and_execute", "anchored_edit")
            graph.add_edge("anchored_edit", END)
            app = graph.compile()

            final = await app.ainvoke({"namespace_id": namespace_id, "principal_id": principal_id})
            print("execute output:", final["execute_output"])
            print("edited to version:", final["new_version"])
        finally:
            await vfs.close()


if __name__ == "__main__":
    asyncio.run(main())
