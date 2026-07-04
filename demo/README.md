# ai-vfs demo

A small, **ephemeral** chat app for exercising the code-mode agent against the VFS — the "sit down and use it" surface for the code-mode agent.

It is **not deployable**.
The store is a throwaway temp-dir VFS that is deleted on shutdown; conversation history lives in the browser tab.
"Works on my machine" is the whole point.

## What's here

- **`backend/`** — a FastAPI app that

  - serves `POST /api/chat` via pydantic-ai's Vercel AI adapter at
    `sdk_version=6` (the version assistant-ui's current runtime speaks), and
  - exposes read-only `GET /api/vfs/tree|file|diff` so the UI can show the live
    VFS the agent is mutating.

The chat route is **stateless**: assistant-ui owns the thread list and sends each thread's full history per request, so multi-conversation isolation holds by construction.
The VFS is the shared world every conversation acts on.

- **`frontend/`** — a Vite + React SPA built on [assistant-ui](https://www.assistant-ui.com) primitives.
  Two panes: the chat on the left, a live VFS inspector (tree + file view + version diff) on the right.
  Built assets land in `backend/static/` so a single process serves both API and UI (one origin, no CORS).

- **MLflow tracing** — the demo stands up an ephemeral `mlflow server` on a throwaway store and points OpenTelemetry at its OTLP endpoint.
  The agent's `Instrumentation` capability emits a span per run, model call, and tool call, so you get a browsable trace tree of what the agent actually did.
  The URL is printed at startup and returned by `GET /api/health` (`mlflow_url`).
  Disable with `AIVFS_MLFLOW=0`; a failure to start (e.g. a busy port) degrades to chat-only rather than stopping the demo.

## Prerequisites

- The code-mode extras: `uv sync --extra codemode` (from the repo root).
- A local OpenAI-compatible model (LM Studio / Ollama / mlx) reachable at `OPENAI_BASE_URL`.
  The chat needs it; the UI and introspection endpoints load without it.
- Node/bun for the one-time frontend build.

## Run it

Build the SPA once, then start the server — one process serves everything:

```bash
# 1. build the frontend into backend/static/
cd demo/frontend && bun install && bun run build && cd ../..

# 2. run the server (from the repo root)
uv run python -m demo.backend
# → http://127.0.0.1:7171
```

### Frontend dev loop (optional)

For hot-reload while editing the UI, run the two dev servers side by side; Vite proxies `/api` to the backend:

```bash
uv run python -m demo.backend        # terminal 1 — API on :7171
cd demo/frontend && bun run dev       # terminal 2 — UI on :5173 (proxies /api)
```

## Configuration

Environment variables (all optional):

| Variable                    | Default                     | Meaning                                            |
| --------------------------- | --------------------------- | -------------------------------------------------- |
| `AIVFS_MODEL`               | `Qwen3.6-27B-4bit`          | model name sent to the endpoint                    |
| `OPENAI_BASE_URL`           | `http://localhost:11434/v1` | OpenAI-compatible endpoint                         |
| `OPENAI_API_KEY`            | `omlx`                      | placeholder; local servers ignore it               |
| `AIVFS_API_STYLE`           | `chat`                      | `chat` or `responses`                              |
| `AIVFS_TOOLS`               | `all`                       | `code`, `files`, or `all` (comma-ok)               |
| `AIVFS_HOST` / `AIVFS_PORT` | `127.0.0.1` / `7171`        | bind address                                       |
| `AIVFS_REPO_ROOT`           | (auto)                      | repo root holding `.specs/` to seed from           |
| `AIVFS_MLFLOW`              | `1`                         | stand up ephemeral MLflow tracing (`0` to disable) |
| `AIVFS_MLFLOW_PORT`         | `5555`                      | MLflow server port                                 |
| `AIVFS_MLFLOW_EXPERIMENT`   | `ai-vfs-demo`               | experiment traces are filed under                  |
