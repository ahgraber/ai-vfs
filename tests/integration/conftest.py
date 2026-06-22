"""Shared setup for ai-vfs integration tests.

Two layers, both keyed off the local compose stack (``tests/integration/docker-compose.yaml``):

**Service endpoints.** The Postgres/Mongo/AWS env vars default to the compose stack when unset
(``setdefault``, so explicit exports win), and each suite skips when its service is unreachable —
so a bare ``uv run pytest tests/integration`` runs whatever is up, and CI without these services
is unaffected. This lives here, not in the Nix flake, because it is test-harness configuration:
it must apply wherever pytest runs (CI, a bare venv, the devshell).

**Stack lifecycle (opt-in).** ``AIVFS_INTEGRATION=1`` makes the harness *manage* the compose
stack for the pytest *session*: it brings the stack up (``compose up -d --wait``) at session start
and tears it down (``compose down -v``) at finish — up once, hot across every test, gone at the
end. ``AIVFS_INTEGRATION=reuse`` instead *reuses* a running stack (bringing one up only if none is
running) and never tears it down — for fast iteration or CI that owns the stack itself. The Nix
devshell's lazy ``podman``/``docker`` shim brings the container machine up on first invocation, so
no manual ``podman machine`` step is needed. Under ``manage``, if a stack is **already running**
the session prompts (reuse / teardown / abort), because teardown is destructive; with no
interactive terminal it aborts rather than clobber a stack it did not start.

**S3/MinIO bucket** is always provisioned *ephemerally* (``pytest_sessionstart`` /
``pytest_sessionfinish``): a per-worker bucket is created clean, pointed at via
``AIVFS_TEST_S3_BUCKET`` before the test modules import, and deleted at session end. Combined
with MinIO's volume-less ``/data``, nothing persists across runs. Per-worker bucket names avoid
xdist collisions. If ``aiobotocore`` is missing or MinIO is unreachable, provisioning is skipped
silently and the S3 tests skip. An explicit ``AIVFS_TEST_S3_BUCKET`` is respected and left
unmanaged.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

# --- service-endpoint defaults → the local compose stack ---
os.environ.setdefault("AIVFS_TEST_POSTGRES_DSN", "postgresql://aivfs:aivfs@localhost:5432/aivfs")
os.environ.setdefault("AIVFS_TEST_MONGO_URI", "mongodb://localhost:27017/aivfs")
# S3/MinIO connection (the bucket itself is provisioned ephemerally below).
os.environ.setdefault("AWS_ACCESS_KEY_ID", "minioadmin")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "minioadmin")
os.environ.setdefault("AWS_ENDPOINT_URL_S3", "http://localhost:9000")
os.environ.setdefault("AWS_REGION", "us-east-1")

# --- opt-in compose-stack lifecycle ---
_INTEGRATION_ENV = "AIVFS_INTEGRATION"
_FALSEY = {"", "0", "false", "no"}
_COMPOSE_FILE = str(Path(__file__).parent / "docker-compose.yaml")

# Set True when this session brought the stack up (or the user accepted adopting a hot one),
# making the session responsible for tearing it down at the end.
_session_owns_stack = False


def _integration_mode() -> str:
    """Resolve ``AIVFS_INTEGRATION``: ``off`` (default), ``reuse``, or ``manage``.

    - ``off`` — harness manages nothing; suites skip when their service is unreachable.
    - ``manage`` (any truthy value other than ``reuse``) — bring a fresh stack up at session
      start and tear it down at finish.
    - ``reuse`` — use a running stack as-is (bring one up only if none is running), never tear down.
    """
    value = os.environ.get(_INTEGRATION_ENV, "").strip().lower()
    if value in _FALSEY:
        return "off"
    if value == "reuse":
        return "reuse"
    return "manage"


def _container_cli() -> str | None:
    """The container CLI to drive compose with (podman preferred; docker is aliased to it)."""
    for cli in ("podman", "docker"):
        if shutil.which(cli):
            return cli
    return None


def _compose(cli: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 — fixed argv, no shell
        [cli, "compose", "-f", _COMPOSE_FILE, *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _stack_running(cli: str) -> bool:
    """True when at least one compose service for this project is already running."""
    result = _compose(cli, "ps", "-q")
    return result.returncode == 0 and bool(result.stdout.strip())


def _compose_up(cli: str) -> None:
    result = _compose(cli, "up", "-d", "--wait")
    if result.returncode != 0:
        pytest.exit(
            f"[ai-vfs] `{cli} compose up` failed:\n{result.stderr}",
            returncode=pytest.ExitCode.INTERNAL_ERROR,
        )


def _prompt_hot_stack(session: pytest.Session) -> str:
    """Ask what to do about an already-running stack: ``reuse``, ``teardown``, or ``abort``.

    Suspends pytest's output capture first so the prompt reaches the terminal (pytest otherwise
    hides stdout and makes ``sys.stdin`` look non-interactive). Returns ``abort`` when there is no
    real terminal — a CI or piped run never adopts, let alone tears down, a stack it did not start.
    """
    capman = session.config.pluginmanager.getplugin("capturemanager")
    if capman is not None:
        capman.suspend_global_capture(in_=True)
    try:
        if sys.stdin is None or not sys.stdin.isatty():
            return "abort"
        print(  # noqa: T201 — operator-facing prompt
            "\n[ai-vfs] The integration compose stack is ALREADY RUNNING.\n"
            "  r = reuse it (leave it running when the session ends)\n"
            "  t = use it, then tear it down at exit (destroys its data)\n"
            "  q = quit without touching it  [default]"
        )
        answer = input("[ai-vfs] choice [r/t/q]: ").strip().lower()
    finally:
        if capman is not None:
            capman.resume_global_capture()
    return {"r": "reuse", "reuse": "reuse", "t": "teardown", "teardown": "teardown"}.get(answer, "abort")


def _ensure_stack(session: pytest.Session, mode: str) -> None:
    """Provision the compose stack per ``mode`` and record teardown ownership (see module docstring)."""
    global _session_owns_stack
    cli = _container_cli()
    if cli is None:
        pytest.exit(
            f"{_INTEGRATION_ENV} is set but no `podman`/`docker` CLI is on PATH",
            returncode=pytest.ExitCode.USAGE_ERROR,
        )
    hot = _stack_running(cli)
    if mode == "reuse":
        if not hot:
            _compose_up(cli)
        return  # reuse never tears down (ownership stays False)
    # mode == "manage"
    if not hot:
        _compose_up(cli)
        _session_owns_stack = True
        return
    choice = _prompt_hot_stack(session)
    if choice == "teardown":
        _session_owns_stack = True
    elif choice == "reuse":
        pass  # adopt the running stack, leave it up at exit
    else:  # abort — never clobber a stack this session did not start
        pytest.exit(
            "[ai-vfs] integration stack already running — left untouched.\n"
            f"  reuse it as-is:  {_INTEGRATION_ENV}=reuse uv run pytest …\n"
            f"  start fresh:     {cli} compose -f {_COMPOSE_FILE} down -v",
            returncode=pytest.ExitCode.USAGE_ERROR,
        )


def _teardown_stack() -> None:
    if not _session_owns_stack:
        return
    cli = _container_cli()
    if cli is not None:
        _compose(cli, "down", "-v")


def _ephemeral_bucket_name() -> str:
    """Per-worker bucket name (S3-valid: lowercase, no underscores)."""
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    return f"aivfs-test-{worker}".lower()


async def _empty_bucket(client, bucket: str) -> None:
    paginator = client.get_paginator("list_objects_v2")
    async for page in paginator.paginate(Bucket=bucket):
        contents = page.get("Contents") or []
        if contents:
            await client.delete_objects(
                Bucket=bucket,
                Delete={"Objects": [{"Key": obj["Key"]} for obj in contents]},
            )


async def _s3_bucket_op(op: str, bucket: str) -> bool:
    """Create-clean (``op="create"``) or delete (``op="delete"``) an ephemeral MinIO bucket.

    Returns True on success. Best-effort: a missing ``aiobotocore`` or any connection/client
    error returns False so the S3 tests skip rather than failing the run.
    """
    if importlib.util.find_spec("aiobotocore") is None:
        return False
    from aiobotocore.session import get_session
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, ClientError

    session = get_session()
    # MinIO requires path-style addressing (no bucket-as-subdomain).
    config = Config(s3={"addressing_style": "path"})
    try:
        async with session.create_client(
            "s3", endpoint_url=os.environ.get("AWS_ENDPOINT_URL_S3"), config=config
        ) as client:
            if op == "create":
                try:
                    await client.create_bucket(Bucket=bucket)
                except ClientError:
                    pass  # already exists (e.g. a crashed prior run) — fall through to clean it
                await _empty_bucket(client, bucket)  # ensure a clean bucket
            elif op == "delete":
                await _empty_bucket(client, bucket)
                try:
                    await client.delete_bucket(Bucket=bucket)
                except ClientError:
                    pass
    except (BotoCoreError, ClientError, OSError):
        return False
    else:
        return True


def _provision_bucket() -> None:
    """Provision an ephemeral per-worker MinIO bucket and point the S3 tests at it.

    An explicit ``AIVFS_TEST_S3_BUCKET`` is respected and left unmanaged.
    """
    if os.environ.get("AIVFS_TEST_S3_BUCKET"):
        return  # caller supplied a bucket — respect it, don't manage its lifecycle
    bucket = _ephemeral_bucket_name()
    if asyncio.run(_s3_bucket_op("create", bucket)):
        os.environ["AIVFS_TEST_S3_BUCKET"] = bucket


def pytest_sessionstart(session: pytest.Session) -> None:
    """Bring the stack up under ``AIVFS_INTEGRATION``, then provision the ephemeral bucket.

    Runs before collection, so the bucket name is in ``AIVFS_TEST_S3_BUCKET`` before the S3 test
    module imports.
    """
    mode = _integration_mode()
    if mode != "off":
        _ensure_stack(session, mode)
    _provision_bucket()


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:  # noqa: ARG001 — pytest hook signature
    """Tear down the ephemeral bucket, then the compose stack if this session started it."""
    bucket = os.environ.get("AIVFS_TEST_S3_BUCKET")
    if bucket and bucket == _ephemeral_bucket_name():
        asyncio.run(_s3_bucket_op("delete", bucket))
    _teardown_stack()  # no-op unless this session took ownership
