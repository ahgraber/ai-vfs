"""Entrypoint: build the ephemeral world + agent, then serve until interrupted.

The VFS and the HTTP server share one event loop on purpose — the store's async
connections are bound to the loop that serves requests, so we construct the world
inside `server.serve()` rather than on a throwaway loop. On shutdown the temp
store is deleted; nothing is left behind.

Run with:  uv run python -m demo.backend
"""

from __future__ import annotations

import asyncio
import pathlib

import setproctitle
import uvicorn

from .agent import build_agent, build_model, registered_tool_names
from .app import create_app
from .config import Settings
from .vfs_setup import build_world, teardown_world

# The built SPA lives next to the backend package; served only if it exists.
STATIC_DIR = pathlib.Path(__file__).parent / "static"


async def serve(settings: Settings) -> None:
    """Construct the world + agent and serve the app on this event loop."""
    repo_root = settings.resolve_repo_root()
    world = await build_world(repo_root)

    # Stand up tracing BEFORE building the agent so its Instrumentation capability
    # inherits the global tracer provider. Best-effort: a tracing failure (e.g. the
    # MLflow port is busy) degrades to chat-only rather than killing the demo.
    tracing = None
    if settings.mlflow_enabled:
        try:
            from .tracing import start_tracing

            tracing = await start_tracing(
                world.tmp_dir, settings.host, settings.mlflow_port, settings.mlflow_experiment
            )
        except Exception as exc:  # noqa: BLE001 — tracing is best-effort; chat must still run
            print(f"mlflow tracing disabled: {exc!r}")

    model = build_model(settings.model_name, settings.openai_base_url, settings.openai_api_key, settings.api_style)
    agent = build_agent(model, settings.enabled_sets)

    app = create_app(
        world,
        agent,
        model_name=settings.model_name,
        enabled_sets=settings.enabled_sets,
        static_dir=STATIC_DIR,
        mlflow_url=tracing.url if tracing else None,
    )

    print(f"repo root : {repo_root}")
    print(f"model     : {settings.model_name} via {settings.openai_base_url} ({settings.api_style})")
    print(f"tools     : {registered_tool_names(settings.enabled_sets)}")
    print(f"spa       : {'served from ' + str(STATIC_DIR) if STATIC_DIR.is_dir() else '(not built; API only)'}")
    if tracing:
        print(f"mlflow    : {tracing.url}  (experiment: {settings.mlflow_experiment!r})")
    else:
        print("mlflow    : (disabled)")
    print(f"listening : http://{settings.host}:{settings.port}")

    config = uvicorn.Config(app, host=settings.host, port=settings.port, log_level="info")
    server = uvicorn.Server(config)
    try:
        await server.serve()
    finally:
        # Stop MLflow before the VFS teardown removes the temp dir its store lives in.
        if tracing:
            from .tracing import stop_tracing

            stop_tracing(tracing)
        await teardown_world(world)
        print(f"torn down world; removed {world.tmp_dir}")


def main() -> None:
    """Set the process title and run the demo server until interrupted."""
    setproctitle.setproctitle("ai-vfs: demo")
    asyncio.run(serve(Settings()))


if __name__ == "__main__":
    main()
