# %% [markdown]
# # ai-vfs — Remote Backends Demo
#
# The same VFS API but constructed against **PostgreSQL + MinIO** from the demo
# Docker Compose stack in `notebooks/docker-compose.yaml`.
#
# **Run model:** open in VS Code Interactive or Jupyter and execute cells
# top-to-bottom with **Shift+Enter**.  Top-level `await` is handled by the kernel
# — do **not** call `asyncio.run()`.
#
# **Environment variables** — the imports cell below applies these demo defaults
# with `setdefault`, so real shell exports (or a different stack) take precedence:
# ```
# AIFS_METADATA_STORE_URI=postgresql://aivfs:aivfs@localhost:55432/aivfs
# AIFS_BLOB_STORE_URI=s3://aivfs-demo
# AWS_ACCESS_KEY_ID=minioadmin
# AWS_SECRET_ACCESS_KEY=minioadmin
# AWS_ENDPOINT_URL_S3=http://localhost:59000
# AWS_REGION=us-east-1
# ```

# %%
from __future__ import annotations

import asyncio
import importlib.util
import logging
import os

from vfs.models import SearchType

logging.basicConfig(level=logging.WARNING)

HAS_ASYNCPG: bool = importlib.util.find_spec("asyncpg") is not None
HAS_AIOBOTOCORE: bool = importlib.util.find_spec("aiobotocore") is not None

# Demo-stack credentials and connection environment. Applied with setdefault so a
# real shell export (or a different stack) wins. botocore reads AWS_* from the
# process environment when the S3 client is first created, so these must be set
# before the VFS is constructed below — otherwise the first write raises
# NoCredentialsError against the cached, credential-less client.
os.environ.setdefault("AIFS_METADATA_STORE_URI", "postgresql://aivfs:aivfs@localhost:55432/aivfs")
os.environ.setdefault("AIFS_BLOB_STORE_URI", "s3://aivfs-demo")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "minioadmin")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "minioadmin")
os.environ.setdefault("AWS_ENDPOINT_URL_S3", "http://localhost:59000")
os.environ.setdefault("AWS_REGION", "us-east-1")

# Demo stack connection parameters (match docker-compose.yaml host ports).
_PG_URI = os.environ.get(
    "AIFS_METADATA_STORE_URI",
    "postgresql://aivfs:aivfs@localhost:55432/aivfs",
)
_S3_BUCKET = os.environ.get("AIFS_BLOB_STORE_URI", "s3://aivfs-demo")
_S3_ENDPOINT = os.environ.get("AWS_ENDPOINT_URL_S3", "http://localhost:59000")

# %% [markdown]
# ## Connectivity probe
#
# This cell checks that the demo stack is reachable and that the optional async
# drivers are installed.  If anything is missing it prints clear actionable
# guidance and sets `services_ok = False`.
#
# **Start the stack before continuing** (podman-first devshell; `docker compose`
# also works once `DOCKER_HOST` points at the podman/colima socket):
# ```
# podman compose -f notebooks/docker-compose.yaml up -d postgres minio
# ```
# One-time MinIO bucket bootstrap (run once after first `up -d`). The `mc`
# commands run *inside* the container, where MinIO listens on 9000 — not the
# 59000 host port:
# ```
# podman compose -f notebooks/docker-compose.yaml exec minio \
#     mc alias set demo http://localhost:9000 minioadmin minioadmin
# podman compose -f notebooks/docker-compose.yaml exec minio \
#     mc mb -p demo/aivfs-demo
# ```
#
# | Service    | Host port | Notes                    |
# |------------|-----------|--------------------------|
# | PostgreSQL | 55432     | `pg_trgm` FTS extension  |
# | MinIO API  | 59000     | S3-compatible object store|
# | MinIO UI   | 59001     | browser console          |
#
# If this cell reports services are down, **stop here** — subsequent cells will
# fail with connection errors.


# %%
async def _tcp_ok(host: str, port: int) -> bool:
    """Return True when the TCP port accepts a connection within 2 s."""
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=2.0)
        writer.close()
        await writer.wait_closed()
    except (OSError, asyncio.TimeoutError):
        return False
    else:
        return True


pg_up = await _tcp_ok("127.0.0.1", 55432)
minio_up = await _tcp_ok("127.0.0.1", 59000)

missing_services = []
if not pg_up:
    missing_services.append("postgres:55432")
if not minio_up:
    missing_services.append("minio:59000")

missing_drivers = []
if not HAS_ASYNCPG:
    missing_drivers.append("asyncpg  →  uv sync --extra postgres")
if not HAS_AIOBOTOCORE:
    missing_drivers.append("aiobotocore  →  uv sync --extra s3")

if missing_services or missing_drivers:
    print("Demo stack not fully available — stop here and fix before continuing.\n")
    if missing_services:
        print("Services not reachable:")
        for s in missing_services:
            print(f"  {s}")
        print("\nStart with:")
        print("  podman compose -f notebooks/docker-compose.yaml up -d postgres minio")
    if missing_drivers:
        print("\nMissing Python drivers:")
        for d in missing_drivers:
            print(f"  {d}")
    services_ok = False
else:
    print(f"Postgres  : reachable ({_PG_URI.rsplit('@', 1)[-1]})")
    print(f"MinIO     : reachable ({_S3_ENDPOINT}  bucket={_S3_BUCKET})")
    services_ok = True

# %% [markdown]
# ## Setup — remote VFS
#
# `VFSConfig` resolves adapters by URI scheme: `postgresql://` →
# `PostgresMetadataStore`, `s3://` → `S3BlobStore`.  Both are lazy-loaded so
# the driver import happens only when the URI prefix is recognized.
#
# **PostgreSQL FTS:** the adapter activates `pg_trgm` (trigram REGEX) and
# `tsvector` BM25 (FULLTEXT) automatically during `initialize()`.  If the role
# lacks `SUPERUSER`, run once manually:
# ```sql
# CREATE EXTENSION IF NOT EXISTS pg_trgm;
# ```

# %%
from vfs import VFS, VFSConfig

config = VFSConfig(
    metadata_store_uri=_PG_URI,
    blob_store_uri=_S3_BUCKET,
    otel_enabled=False,
    audit_log_enabled=False,
)
vfs = VFS(config)
await vfs.initialize()

ns = await vfs.create_namespace("remote-demo", "system")
admin = await vfs.create_principal("admin", principal_type="user")
await vfs.bootstrap_admin(principal_id=admin.id, namespace_id=ns.id)
await vfs.grant(
    granter_id=admin.id,
    target_principal_id=admin.id,
    namespace_id=ns.id,
    path_prefix="/",
    operations={"read", "write", "delete", "execute"},
)

ns_id = ns.id
admin_id = admin.id
print(f"namespace : {ns_id}")
print(f"admin     : {admin_id}")

# %% [markdown]
# ## Files & Versions — S3 blob round-trip
#
# The write path computes a BLAKE3 hash client-side, PUT the blob to MinIO via
# the S3 API, and records the version in Postgres.  Read resolves the hash from
# Postgres then GETs the blob from MinIO — a full round-trip through both backends.
# Try changing the content bytes and re-running both cells to watch versions
# accumulate in Postgres.

# %%
v1 = await vfs.write(
    namespace_id=ns_id,
    path="/remote/hello.txt",
    content=b"Hello from S3!\n",
    principal_id=admin_id,
)
print(f"write v{v1.version_number}  hash={v1.content_hash[:12]}…  (blob stored in MinIO)")

# %%
content = await vfs.read(
    namespace_id=ns_id,
    path="/remote/hello.txt",
    principal_id=admin_id,
)
print(f"read      : {content!r}")
# Prove the full Postgres → MinIO round-trip returned the original bytes.
round_trip_ok = content == b"Hello from S3!\n"
print(f"round-trip: content == original → {round_trip_ok}")

v2 = await vfs.write(
    namespace_id=ns_id,
    path="/remote/hello.txt",
    content=b"Updated in S3!\n",
    principal_id=admin_id,
)
print(f"write v{v2.version_number}  hash={v2.content_hash[:12]}…  (new blob in MinIO)")

history = await vfs.versions(
    namespace_id=ns_id,
    path="/remote/hello.txt",
    principal_id=admin_id,
)
print(f"versions  : {[(v.version_number, v.content_hash[:12] + '…') for v in history]}  (newest first)")

# %% [markdown]
# ## Search — Postgres FTS
#
# PostgreSQL uses `pg_trgm` for REGEX (trigram similarity index) and `tsvector`
# for FULLTEXT (BM25 ranking via `ts_rank`).  Both are zero-blob-reads when the
# index is current.  Content is indexed inside each `write()` call atomically —
# no manual reindex needed on a fresh namespace.

# %%
await vfs.write(
    namespace_id=ns_id,
    path="/src/main.py",
    content=b"def main():\n    print('hello postgres')\n",
    principal_id=admin_id,
)
await vfs.write(
    namespace_id=ns_id,
    path="/src/utils.py",
    content=b"def helper():\n    return 42\n",
    principal_id=admin_id,
)
print("seeded /src/main.py and /src/utils.py")

# %%
# REGEX via pg_trgm — zero blob reads when index is fresh.
regex_results = await vfs.search(
    namespace_id=ns_id,
    query=r"def \w+\(\)",
    scope="/src/",
    search_type=SearchType.REGEX,
    principal_id=admin_id,
)
print(f"REGEX 'def \\w+()':  {len(regex_results)} hit(s)")
for r in sorted(regex_results, key=lambda x: (x.path, x.line_number or 0)):
    print(f"  {r.path}:{r.line_number}  {r.match_context!r}")

# %%
# FULLTEXT BM25 — tsvector ranking, zero blob reads.
ft_results = await vfs.search(
    namespace_id=ns_id,
    query="hello postgres",
    scope="/",
    search_type=SearchType.FULLTEXT,
    principal_id=admin_id,
)
print(f"FULLTEXT 'hello postgres':  {len(ft_results)} hit(s)  (BM25-ranked)")
for r in ft_results:
    print(f"  {r.path}  score={r.score:.4f}")

# %% [markdown]
# ## Cleanup
#
# Close the VFS to release the asyncpg connection pool and the aiobotocore
# session cleanly.

# %%
await vfs.close()
print("remote VFS connections closed")
