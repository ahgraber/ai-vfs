# ai-vfs Notebooks

Hands-on demos for the ai-vfs library.

## Notebooks

| File                         | Description                                          | Infrastructure           |
| ---------------------------- | ---------------------------------------------------- | ------------------------ |
| `01_vfs_tour.py`             | Full VFS API walkthrough                             | None (SQLite + local FS) |
| `02_remote_backends.py`      | Postgres + MinIO tour                                | Demo stack required      |
| `03_pydantic_ai_codemode.py` | Sandbox execute + native write as a pydantic-ai tool | None (SQLite + local FS) |

`langgraph_smoke.py` is a disposable falsification sketch (does the public surface
plug into a LangGraph node?), not a shipped integration; it needs `langgraph`
installed to run and is excluded from the suite.

## Run model — interactive-first

Both notebooks are written for **top-level `await`**: the kernel owns the event loop and executes each cell directly.
Do **not** call `asyncio.run()` inside a running kernel — it will raise `RuntimeError`.

### VS Code Interactive

1. Open any `.py` notebook file in VS Code.
2. Execute cells top-to-bottom with **Shift+Enter** (or click **Run Cell**).
3. Each cell's output appears inline immediately — stop, edit a literal, and
   re-run any cell independently.

### Jupyter

```bash
uv run jupyter notebook
```

The `.py` files use [jupytext](https://jupytext.readthedocs.io/) percent format.
Pair once to keep a `.ipynb` in sync:

```bash
uv run jupytext --set-formats py:percent,ipynb notebooks/01_vfs_tour.py
```

### Headless smoke-test (CI / quick check)

```bash
uv run jupytext --to ipynb --execute \
    --output /tmp/01_vfs_tour.ipynb \
    notebooks/01_vfs_tour.py
```

`pydantic-monty` is included in the default `uv sync` so the Monty sandbox cells run automatically.
No `.ipynb` is committed — the `#%%` `.py` file is the canonical artifact.

## Demo stack (notebook 02)

### Port table

| Service    | Demo host port | Test host port | Notes            |
| ---------- | -------------- | -------------- | ---------------- |
| PostgreSQL | 55432          | 5432           | `pg_trgm` FTS    |
| MinIO API  | 59000          | 9000           | S3-compatible    |
| MinIO UI   | 59001          | 9001           | browser console  |
| MongoDB    | 57017          | 27017          | optional profile |

The demo ports are intentionally different from `tests/integration/docker-compose.yaml`
so both stacks can run simultaneously.

### Start / stop

The Nix devshell is podman-first, so these examples use `podman compose` (it
configures the container socket itself). `docker compose` also works once
`DOCKER_HOST` points at the podman or colima socket — the devshell's shellHook
exports that automatically when a machine is running.

The `mc` bootstrap commands run **inside** the MinIO container, where the S3 API
listens on `9000`. `59000` is only the host port; use it from the host (e.g. the
notebook's `AWS_ENDPOINT_URL_S3`), not inside `exec`.

```bash
# Start Postgres + MinIO
podman compose -f notebooks/docker-compose.yaml up -d postgres minio

# One-time MinIO bucket bootstrap (runs inside the container → port 9000)
podman compose -f notebooks/docker-compose.yaml exec minio \
    mc alias set demo http://localhost:9000 minioadmin minioadmin
podman compose -f notebooks/docker-compose.yaml exec minio \
    mc mb -p demo/aivfs-demo

# Enable pg_trgm for FTS (one-time; requires superuser)
podman compose -f notebooks/docker-compose.yaml exec postgres \
    psql -U aivfs -c "CREATE EXTENSION IF NOT EXISTS pg_trgm;"

# Start optional MongoDB
podman compose -f notebooks/docker-compose.yaml --profile mongo up -d mongo

# Tear down (removes volumes)
podman compose -f notebooks/docker-compose.yaml down -v
```

### Environment variables for notebook 02

```bash
export AIFS_METADATA_STORE_URI='postgresql://aivfs:aivfs@localhost:55432/aivfs'
export AIFS_BLOB_STORE_URI='s3://aivfs-demo'
export AWS_ACCESS_KEY_ID='minioadmin'
export AWS_SECRET_ACCESS_KEY='minioadmin'
export AWS_ENDPOINT_URL_S3='http://localhost:59000'
export AWS_REGION='us-east-1'
```
