# ai-vfs demo

A small, **ephemeral** chat app for driving the code-mode agent against the VFS by hand — the "sit down and use it" surface.

It is **not deployable**, and that is the point.
The store is a throwaway temp-dir VFS deleted on shutdown; conversation history lives in the browser tab.
"Works on my machine" is the whole point.

## Three parts: backend, frontend, tracing

- **`backend/`** — a FastAPI app that

  - serves `POST /api/chat` via pydantic-ai's Vercel AI adapter at
    `sdk_version=6` (the version assistant-ui's current runtime speaks),
  - exposes read-only `GET /api/vfs/tree|file|diff` so the UI can show the live
    VFS the agent is mutating, and
  - accepts `POST /api/vfs/upload` (multipart) to write a file into the VFS under `/uploads/<name>` — drop in an arbitrary file type and watch how it lands (text is indexed for search; binary is stored as opaque bytes and shows up as replacement characters in the inspector and to the agent's text tools).
    For registered binary types the upload also writes an extracted-text **sidecar** next to the original (`foo.pdf` → `foo.pdf.md`), so the agent can read and search the content with the ordinary tools; see [content extraction](#uploaded-binaries-become-searchable-through-sidecars).

The chat route is **stateless**: assistant-ui owns the thread list and sends each thread's full history per request, so multi-conversation isolation holds by construction.
The VFS is the shared world every conversation acts on.

- **`frontend/`** — a Vite + React SPA built on [assistant-ui](https://www.assistant-ui.com) primitives.
  Two panes: the chat on the left, a live VFS inspector (tree + file view + version diff, plus an **upload** button) on the right.
  Built assets land in `backend/static/`, so one process serves both the API and the UI (one origin, no CORS).

- **MLflow tracing** — the demo stands up an ephemeral `mlflow server` on a throwaway store and points OpenTelemetry at its OTLP endpoint.
  The agent's `Instrumentation` capability emits a span per run, model call, and tool call, so the trace tree shows exactly what the agent did.
  The URL prints at startup and returns from `GET /api/health` (`mlflow_url`).
  Disable it with `AIVFS_MLFLOW=0`; a failed start (a busy port, say) degrades to chat-only rather than stopping the demo.

## Uploaded binaries become searchable through sidecars

Binary documents are stored as opaque bytes, so a PDF is unreadable and unsearchable to the agent's text tools on its own.
`backend/extract.py` adds a small ports-and-adapters seam that fixes this at the upload boundary: a `ContentExtractor` port, adapters resolved by file extension through a lazy registry (mirroring the core provider registries), and one or more sidecar files written next to the original.
An adapter may emit more than one artifact — a spreadsheet becomes one CSV sidecar per sheet.

Registered types:

| Extension        | Adapter               | Sidecar(s)                        | Notes                                                                                                                                                 |
| ---------------- | --------------------- | --------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| `.pdf`           | `PdfExtractor`        | `foo.pdf.md`                      | [liteparse](https://pypi.org/project/liteparse/), OCR off — reads the embedded text layer.                                                            |
| `.docx`, `.pptx` | `OfficeTextExtractor` | `foo.docx.txt`                    | liteparse when the `soffice` (LibreOffice) binary is present — it converts via LibreOffice, preserving tables; otherwise a stdlib zip+XML text strip. |
| `.xlsx`, `.xlsm` | `XlsxExtractor`       | `book.xlsx.<sheet>.csv` per sheet | [openpyxl](https://pypi.org/project/openpyxl/), one CSV per worksheet.                                                                                |

**Spreadsheet values** — `XlsxExtractor` reads with `data_only=True`, so a cell shows the value **cached** by the app that last saved the file. openpyxl has no formula engine, so a formula whose result was never cached (e.g. a programmatically generated workbook) reads as an empty cell rather than being recomputed.
Read-time recalculation would require routing through a calc engine (LibreOffice when `soffice` is present); it is deliberately not done here.

Extraction runs at ingest, not inside `vfs.write`, so the VFS contract is untouched.
It is best-effort: the original is always stored, and an extraction failure is reported (as `extract_error`) without failing the upload.
Untrusted XML is parsed with `defusedxml` to guard the ingest boundary against entity-expansion bombs.

To add a type: write an adapter with `suffixes()` and `async extract()`, then add one row to `_EXTRACTORS` keyed by the lowercased extension.
Dispatch is by extension because it is authoritative on upload and is the only key that separates the ZIP-container Office formats (`.xlsx`/`.docx`/`.pptx` share magic bytes).

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

| Variable                    | Default                     | Meaning                                                                                                                                                                          |
| --------------------------- | --------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `AIVFS_MODEL`               | `Qwen3.6-27B-4bit`          | model name sent to the endpoint                                                                                                                                                  |
| `OPENAI_BASE_URL`           | `http://localhost:11434/v1` | OpenAI-compatible endpoint                                                                                                                                                       |
| `OPENAI_API_KEY`            | `omlx`                      | placeholder; local servers ignore it                                                                                                                                             |
| `AIVFS_API_STYLE`           | `chat`                      | `chat` or `responses`                                                                                                                                                            |
| `AIVFS_CONTEXT_TOKENS`      | `32768`                     | context-window fallback; the real value is probed from the endpoint at startup (`omlx` exposes it as `max_model_len`) and this is used only when the endpoint doesn't report one |
| `AIVFS_COMPACT_FRACTION`    | `0.6`                       | fraction of the context window a request may reach before older history is summarized                                                                                            |
| `AIVFS_TOOLS`               | `all`                       | `code`, `files`, or `all` (comma-ok)                                                                                                                                             |
| `AIVFS_HOST` / `AIVFS_PORT` | `127.0.0.1` / `7171`        | bind address                                                                                                                                                                     |
| `AIVFS_REPO_ROOT`           | (auto)                      | repo root holding `.specs/` to seed from                                                                                                                                         |
| `AIVFS_MLFLOW`              | `1`                         | stand up ephemeral MLflow tracing (`0` to disable)                                                                                                                               |
| `AIVFS_MLFLOW_PORT`         | `5555`                      | MLflow server port                                                                                                                                                               |
| `AIVFS_MLFLOW_EXPERIMENT`   | `ai-vfs-demo`               | experiment traces are filed under                                                                                                                                                |
