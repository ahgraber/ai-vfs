"""FastAPI app: one chat endpoint, read-only VFS introspection, and the built SPA.

The chat route hands the request straight to pydantic-ai's Vercel adapter at
`sdk_version=6` (v5 is the library default and lands assistant-ui on its degraded
legacy path). The route is stateless: assistant-ui owns the thread list and sends
each thread's full history per request, so multi-conversation isolation holds by
construction. The shared, mutable VFS is the common world every conversation acts
on; the introspection routes read it through a full-visibility principal.
"""

from __future__ import annotations

import json
import pathlib

from pydantic_ai import Agent
from pydantic_ai.ui.vercel_ai import VercelAIAdapter

from fastapi import FastAPI, HTTPException, Query, Request, UploadFile
from fastapi.staticfiles import StaticFiles

from vfs import VFS, Session

from .agent import AgentDeps, registered_tool_names
from .extract import resolve_extractor
from .introspect import diff as vfs_diff, read_file as vfs_read_file, tree as vfs_tree
from .vfs_setup import DemoWorld

# Cap upload size at the boundary; this is a local demo, not a service.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


def create_app(
    world: DemoWorld,
    agent: Agent[AgentDeps, str],
    *,
    model_name: str,
    enabled_sets: set[str],
    static_dir: pathlib.Path | None = None,
    mlflow_url: str | None = None,
) -> FastAPI:
    """Build the demo app around an already-constructed world and agent."""
    app = FastAPI(title="ai-vfs demo", docs_url="/api/docs", openapi_url="/api/openapi.json")
    deps = AgentDeps(vfs=world.vfs, namespace_id=world.namespace_id, principal_id=world.agent_id)
    vfs: VFS = world.vfs

    @app.get("/api/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "model": model_name,
            "tools": registered_tool_names(enabled_sets),
            "namespace_id": world.namespace_id,
            "mlflow_url": mlflow_url,
        }

    @app.post("/api/chat")
    async def chat(request: Request):
        # The client sends a stable per-thread id as the request `id`; thread it
        # through as the conversation id so every turn in a conversation shares one
        # id (else pydantic-ai generates a fresh one per turn and traces never
        # group). Reading the body here caches it for dispatch_request to re-read.
        conversation_id: str | None = None
        try:
            conversation_id = json.loads(await request.body()).get("id")
        except (ValueError, AttributeError):
            pass
        # sdk_version=6 matches assistant-ui's current runtime; manage_system_prompt
        # 'server' strips any client-forwarded system prompt (handled, not broken).
        return await VercelAIAdapter.dispatch_request(
            request,
            agent=agent,
            deps=deps,
            sdk_version=6,
            manage_system_prompt="server",
            conversation_id=conversation_id,
        )

    @app.get("/api/vfs/tree")
    async def get_tree(prefix: str = Query(default="/")) -> dict:
        paths = await vfs_tree(vfs, world.namespace_id, world.admin_id, prefix)
        return {"prefix": prefix, "paths": paths}

    @app.get("/api/vfs/file")
    async def get_file(path: str = Query(...), version: int | None = Query(default=None)) -> dict:
        return await vfs_read_file(vfs, world.namespace_id, world.admin_id, path, version_number=version)

    @app.get("/api/vfs/diff")
    async def get_diff(
        path: str = Query(...),
        older: int | None = Query(default=None),
        newer: int | None = Query(default=None),
    ) -> dict:
        return await vfs_diff(vfs, world.namespace_id, world.admin_id, path, older=older, newer=newer)

    @app.post("/api/vfs/upload")
    async def upload(file: UploadFile) -> dict:
        # A user action, so it writes as `admin`; the agent still sees it via its
        # `/` read grant. Content is stored as opaque bytes regardless of type —
        # search indexing and text tools decode UTF-8 downstream, so binary lands
        # as a stored-but-unsearchable blob rather than an error here.
        content = await file.read()
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail=f"file exceeds {MAX_UPLOAD_BYTES} bytes")
        name = pathlib.PurePosixPath(file.filename or "upload.bin").name  # strip any client path
        if not name:
            raise HTTPException(status_code=400, detail="missing filename")
        vfs_path = f"/uploads/{name}"
        session = Session(vfs, world.namespace_id, world.admin_id)
        meta = await session.write(vfs_path, content)
        result = {"path": vfs_path, "version_number": meta.version_number, "size": meta.size}

        # For registered binary types, write searchable/readable text sidecars next to
        # the original (foo.pdf -> foo.pdf.md; book.xlsx -> book.xlsx.<sheet>.csv per
        # sheet). The original is already stored, so extraction is best-effort: any
        # failure degrades to "original only" rather than losing the upload.
        try:
            extractor = resolve_extractor(pathlib.PurePosixPath(name).suffix)
            if extractor is not None:
                derived_paths: list[str] = []
                for artifact in await extractor.extract(content):
                    derived_path = vfs_path + artifact.suffix
                    await session.write(derived_path, artifact.text.encode("utf-8"))
                    derived_paths.append(derived_path)
                if derived_paths:
                    result["derived_paths"] = derived_paths
        except Exception as exc:  # noqa: BLE001 - best-effort extraction must not fail a stored upload
            result["extract_error"] = f"{type(exc).__name__}: {exc}"
        return result

    # Mount the built SPA last so it catches only what the API routes above didn't.
    if static_dir is not None and static_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="spa")

    return app
