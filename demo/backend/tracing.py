"""Ephemeral MLflow tracing for the demo.

Spawns a throwaway `mlflow server` on a temp SQLite store and points the global
OpenTelemetry tracer provider at its OTLP endpoint (`/v1/traces`). Any pydantic-ai
agent built afterward inherits that provider via its `Instrumentation` capability,
so every run, model call, and tool call becomes a browsable span tree. The
subprocess and its store are torn down on shutdown; nothing is left behind.

Imported lazily by the entrypoint so the demo can run without MLflow/OTel when
`AIVFS_MLFLOW=0`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
import subprocess

import httpx
import mlflow
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import set_tracer_provider


@dataclass
class Tracing:
    """Handles for the running MLflow server and the OTel provider feeding it."""

    proc: subprocess.Popen
    provider: TracerProvider
    url: str
    experiment_id: str


async def start_tracing(tmp_dir: str, host: str, port: int, experiment: str) -> Tracing:
    """Launch `mlflow server`, wait until it answers, and wire OTel to its OTLP endpoint."""
    proc = subprocess.Popen(  # noqa: S603 — fixed argv, no shell, no untrusted input
        [
            "mlflow",
            "server",
            "--backend-store-uri",
            f"sqlite:///{tmp_dir}/mlflow.db",
            "--default-artifact-root",
            f"{tmp_dir}/mlartifacts",
            "--host",
            host,
            "--port",
            str(port),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base_url = f"http://{host}:{port}"
    async with httpx.AsyncClient(timeout=2.0) as client:
        for _ in range(60):
            if proc.poll() is not None:
                raise RuntimeError(f"mlflow server exited early (code {proc.returncode}); is port {port} free?")
            try:
                resp = await client.get(base_url + "/")
            except httpx.TransportError:
                await asyncio.sleep(0.5)
                continue
            if resp.status_code < 500:
                break
            await asyncio.sleep(0.5)
        else:
            proc.terminate()
            raise RuntimeError(f"mlflow server did not become reachable at {base_url} within 30s")

    mlflow.set_tracking_uri(base_url)
    mlflow.set_experiment(experiment)
    experiment_id = mlflow.get_experiment_by_name(experiment).experiment_id

    # OTLPSpanExporter reads these at construction, so set them first.
    os.environ["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] = f"{base_url}/v1/traces"
    os.environ["OTEL_EXPORTER_OTLP_TRACES_HEADERS"] = f"x-mlflow-experiment-id={experiment_id}"
    provider = TracerProvider()
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    set_tracer_provider(provider)

    return Tracing(proc=proc, provider=provider, url=base_url, experiment_id=experiment_id)


def stop_tracing(tracing: Tracing) -> None:
    """Flush and shut down the tracer, then terminate the MLflow subprocess."""
    tracing.provider.shutdown()
    tracing.proc.terminate()
    try:
        tracing.proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        tracing.proc.kill()
