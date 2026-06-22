# Running Integration Tests

The integration tests require live Postgres, MongoDB, and MinIO instances.
Each suite skips automatically when its service is unreachable, so a partial stack — or no stack — simply runs fewer tests.

## Run

From the Nix devshell, opt in with `AIVFS_INTEGRATION` and the harness owns the stack for the session — it brings the compose stack up, runs every test against it, and tears it down at the end:

```sh
AIVFS_INTEGRATION=1 uv run pytest tests/integration -q
```

No `podman` commands, env-var exports, or bucket creation are needed:

- The devshell's lazy `podman`/`docker` shim brings the container machine up on first use (no `podman machine init`/`start`).
- `conftest.py` runs `compose up -d --wait` at session start and `compose down -v` at session finish.
- `conftest.py` also defaults the service DSNs and provisions an ephemeral, per-worker MinIO bucket, deleting it at the end.

If a stack is **already running**, the session prompts — `r` reuse it (left running), `t` tear it down at exit, `q` quit — because teardown is destructive; with no interactive terminal it aborts rather than touch a stack it did not start.

To reuse a running stack without any teardown (fast iteration, or CI that owns the stack), use `reuse` instead of `1`:

```sh
AIVFS_INTEGRATION=reuse uv run pytest tests/integration -q
```

## Run against your own services

Without `AIVFS_INTEGRATION`, the harness manages nothing — it points at whatever is reachable and skips the rest:

```sh
uv run pytest tests/integration -q
```

Export the matching env vars to target services you manage yourself; `setdefault` means your values take precedence, and an explicit `AIVFS_TEST_S3_BUCKET` is left unmanaged (conftest neither creates nor deletes it).

| Service  | Port  | Env var (defaulted by conftest)  |
| -------- | ----- | -------------------------------- |
| Postgres | 5432  | `AIVFS_TEST_POSTGRES_DSN`        |
| MongoDB  | 27017 | `AIVFS_TEST_MONGO_URI`           |
| MinIO    | 9000  | `AIVFS_TEST_S3_BUCKET` + `AWS_*` |

To bring the stack up manually for inspection or fast iteration (e.g. keeping it hot across several runs), use compose directly and run without `AIVFS_INTEGRATION`:

```sh
podman compose -f tests/integration/docker-compose.yaml up -d
uv run pytest tests/integration -q
podman compose -f tests/integration/docker-compose.yaml down -v
```

See `docker-compose.yaml` for full service definitions and port mappings.

## Parallel execution

The integration tests are compatible with `pytest-xdist`; each worker provisions its own bucket/database namespace to avoid collisions:

```sh
AIVFS_INTEGRATION=1 uv run pytest -n auto tests/integration -q
```
