"""Hermetic smoke test for the demo backend.

Uses pydantic-ai's `TestModel` so nothing here needs a live model server: it
pins that the chat route streams the Vercel AI v6 wire format (including
tool-call parts) and that the read-only introspection routes see the seeded VFS.
The live-model path is verified separately by the gate; this guards the wiring
and the v6 protocol contract in CI.
"""

from __future__ import annotations

import json
import pathlib

from demo.backend.agent import _render_hits, _render_lines, build_agent
from demo.backend.app import create_app
from demo.backend.vfs_setup import build_world, teardown_world
from httpx import ASGITransport, AsyncClient
from pydantic_ai.models.test import TestModel
import pytest
import pytest_asyncio

from vfs.models import SearchResult

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

FILE_TEXT = "alpha\nbravo\ncharlie\ndelta\n"


def test_render_lines_without_bounds_returns_full_text():
    assert _render_lines(FILE_TEXT, None, None) == FILE_TEXT


def test_render_lines_span_is_inclusive_and_numbered():
    assert _render_lines(FILE_TEXT, 2, 3) == "2\tbravo\n3\tcharlie"


def test_render_lines_open_bounds_default_to_file_extent():
    assert _render_lines(FILE_TEXT, None, 2) == "1\talpha\n2\tbravo"
    assert _render_lines(FILE_TEXT, 3, None) == "3\tcharlie\n4\tdelta"


def test_render_lines_clamps_out_of_range_bounds():
    assert _render_lines(FILE_TEXT, 0, 99) == "1\talpha\n2\tbravo\n3\tcharlie\n4\tdelta"


def test_render_lines_empty_span_reports_file_length():
    assert _render_lines(FILE_TEXT, 10, 20) == "(no lines in requested span; file has 4 lines)"


def test_render_hits_includes_line_and_context_when_present():
    hits = [
        SearchResult(path="/a.md", line_number=7, match_context="found here"),
        SearchResult(path="/b.md"),
    ]
    assert _render_hits(hits) == "/a.md:7: found here\n/b.md"


def test_render_hits_empty_reports_no_matches():
    assert _render_hits([]) == "(no matches)"


@pytest_asyncio.fixture
async def client_and_world():
    world = await build_world(REPO_ROOT)
    # call_tools=['list_dir'] exercises one safe, no-mutation tool so the stream
    # carries tool-input/tool-output parts without depending on a real model.
    agent = build_agent(TestModel(call_tools=["list_dir"]), {"files"})
    app = create_app(world, agent, model_name="test-model", enabled_sets={"files"})
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://demo.test") as client:
        yield client, world
    await teardown_world(world)


@pytest.mark.asyncio
async def test_health_reports_model_and_tools(client_and_world):
    client, _ = client_and_world
    resp = await client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["model"] == "test-model"
    assert "read_file" in body["tools"] and "list_dir" in body["tools"]


@pytest.mark.asyncio
async def test_introspection_sees_seeded_specs(client_and_world):
    client, _ = client_and_world
    tree = (await client.get("/api/vfs/tree", params={"prefix": "/"})).json()
    assert "/NORTH-STAR.md" in tree["paths"]

    north_star = (await client.get("/api/vfs/file", params={"path": "/NORTH-STAR.md"})).json()
    assert north_star["content"]
    assert north_star["version_number"] is not None


@pytest.mark.asyncio
async def test_chat_streams_v6_tool_and_text_parts(client_and_world):
    client, _ = client_and_world
    body = {
        "trigger": "submit-message",
        "id": "conv-1",
        "messages": [
            {"id": "m1", "role": "user", "parts": [{"type": "text", "text": "List everything under /specs."}]}
        ],
    }
    seen: set[str] = set()
    async with client.stream("POST", "/api/chat", json=body) as resp:
        assert resp.status_code == 200
        async for line in resp.aiter_lines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[len("data:") :].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                seen.add(json.loads(payload).get("type", ""))
            except json.JSONDecodeError:
                continue

    # v6 tool-call lifecycle and a final text stream must both be present.
    assert "tool-input-available" in seen
    assert "tool-output-available" in seen
    assert {"text-start", "text-delta"} & seen
    assert "finish" in seen
