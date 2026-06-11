# Running Integration Tests

The integration tests require live Postgres, MongoDB, and MinIO instances.
They are skipped automatically in the default test run when the required environment variables are absent.

## One-command run

```sh
scripts/integration-tests.sh
```

The script resolves a container engine (podman → colima → docker), starts the compose stack, creates the MinIO bucket, exports the env vars, runs pytest, and tears down the stack on exit.
See `--help` for all options:

```sh
scripts/integration-tests.sh --help
```

Useful flags:

| Flag        | Effect                                                   |
| ----------- | -------------------------------------------------------- |
| `--init`    | Allow `podman machine init` when no VM exists (one-time) |
| `--keep-up` | Leave containers running after the test run              |
| `--stop-vm` | Also stop the container VM after teardown                |
| `--`        | Everything after this is forwarded to pytest             |

Examples:

```sh
# Run only S3 blob tests
scripts/integration-tests.sh -- -k s3_blob

# Run with verbose pytest output, keep containers up for inspection
scripts/integration-tests.sh --keep-up -- -x -v

# First run on a machine without a podman VM
scripts/integration-tests.sh --init
```

## Manual / step-by-step

If you prefer to manage the stack yourself:

```sh
# 1. Start the stack
docker compose -f tests/integration/docker-compose.yaml up -d

# 2. Create the MinIO bucket (one-time, idempotent)
docker compose -f tests/integration/docker-compose.yaml exec minio \
    mc alias set local http://localhost:9000 minioadmin minioadmin
docker compose -f tests/integration/docker-compose.yaml exec minio \
    mc mb -p local/aivfs-test

# 3. Export env vars
export AIVFS_TEST_POSTGRES_DSN=postgresql://aivfs:aivfs@localhost:5432/aivfs
export AIVFS_TEST_MONGO_URI=mongodb://localhost:27017/aivfs
export AIVFS_TEST_S3_BUCKET=aivfs-test
export AWS_ACCESS_KEY_ID=minioadmin
export AWS_SECRET_ACCESS_KEY=minioadmin
export AWS_ENDPOINT_URL_S3=http://localhost:9000
export AWS_REGION=us-east-1

# 4. Run tests
uv run pytest tests/integration -q

# 5. Tear down
docker compose -f tests/integration/docker-compose.yaml down -v
```

## Service details

See `docker-compose.yaml` for full service definitions and port mappings.

| Service  | Port  | Env var                          |
| -------- | ----- | -------------------------------- |
| Postgres | 5432  | `AIVFS_TEST_POSTGRES_DSN`        |
| MongoDB  | 27017 | `AIVFS_TEST_MONGO_URI`           |
| MinIO    | 9000  | `AIVFS_TEST_S3_BUCKET` + `AWS_*` |

## Parallel execution

The integration tests are compatible with `pytest-xdist`.
Each worker provisions its own database/key-prefix to avoid collisions:

```sh
uv run pytest -n auto tests/integration -q
```
