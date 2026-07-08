"""Hermetic smoke test for the demo backend.

Uses pydantic-ai's `TestModel` so nothing here needs a live model server: it
pins that the chat route streams the Vercel AI v6 wire format (including
tool-call parts) and that the read-only introspection routes see the seeded VFS.
The live-model path is verified separately by the gate; this guards the wiring
and the v6 protocol contract in CI.
"""

from __future__ import annotations

import io
import json
import pathlib
import zipfile

from demo.backend.agent import READ_DEFAULT_LINES, _render_hits, _render_lines, build_agent
from demo.backend.app import MAX_UPLOAD_BYTES, create_app
import demo.backend.extract as extract_mod
from demo.backend.extract import (
    ContentExtractor,
    OfficeTextExtractor,
    _strip_ooxml_text,
    _trim_trailing_blank_rows,
    resolve_extractor,
)
from demo.backend.history import SUMMARY_MARKER, _safe_cut, build_compactor
import demo.backend.model_info as model_info_mod
from demo.backend.model_info import resolve_context_window
from demo.backend.vfs_setup import build_world, teardown_world
import httpx
from httpx import ASGITransport, AsyncClient
from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.test import TestModel
import pytest
import pytest_asyncio

from vfs import Session
from vfs.models import SearchResult

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

FILE_TEXT = "alpha\nbravo\ncharlie\ndelta\n"


def _minimal_pdf(text: str) -> bytes:
    """A one-page PDF with `text` in its text layer and a valid xref — enough for liteparse."""
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 144] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
    ]
    stream = b"BT /F1 24 Tf 20 100 Td (%s) Tj ET" % text.encode("ascii")
    objs.append(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream))
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    pdf = b"%PDF-1.4\n"
    offsets: list[int] = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(pdf))
        pdf += b"%d 0 obj\n%s\nendobj\n" % (i, body)
    xref_pos = len(pdf)
    pdf += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        pdf += b"%010d 00000 n \n" % off
    pdf += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref_pos)
    return pdf


_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

_OOXML = "http://schemas.openxmlformats.org/"


def _zip(parts: dict[str, str]) -> bytes:
    """Zip a mapping of archive-name -> XML string into an OOXML package."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, body in parts.items():
            archive.writestr(name, body)
    return buffer.getvalue()


def _docx(text: str) -> bytes:
    """A minimal but structurally valid .docx with one paragraph of `text`."""
    return _zip(
        {
            "[Content_Types].xml": f'<?xml version="1.0"?><Types xmlns="{_OOXML}package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument'
            '.wordprocessingml.document.main+xml"/></Types>',
            "_rels/.rels": f'<?xml version="1.0"?><Relationships xmlns="{_OOXML}package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{_OOXML}officeDocument/2006/relationships/officeDocument" '
            'Target="word/document.xml"/></Relationships>',
            "word/document.xml": f'<?xml version="1.0"?><w:document xmlns:w="{_OOXML}wordprocessingml/2006/main">'
            f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>",
        }
    )


def _pptx(text: str) -> bytes:
    """A .pptx package with a single slide part holding `text` (enough for the stdlib strip)."""
    return _zip(
        {
            "ppt/slides/slide1.xml": f'<?xml version="1.0"?><p:sld xmlns:p="{_OOXML}presentationml/2006/main" '
            f'xmlns:a="{_OOXML}drawingml/2006/main"><p:cSld><p:spTree><p:sp><p:txBody>'
            f"<a:p><a:r><a:t>{text}</a:t></a:r></a:p>"
            "</p:txBody></p:sp></p:spTree></p:cSld></p:sld>",
        }
    )


def _xlsx_with_formulas() -> bytes:
    """A one-sheet ("Data") .xlsx: a cached formula (=B1*2 -> 200) and an uncached one (=B1+1)."""
    main = f"{_OOXML}spreadsheetml/2006/main"
    rel = f"{_OOXML}officeDocument/2006/relationships"
    rows = (
        '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1"><v>100</v></c></row>'
        '<row r="2"><c r="A2"><v>7</v></c>'
        '<c r="B2"><f>B1*2</f><v>200</v></c>'  # cached result present
        '<c r="C2"><f>B1+1</f></c></row>'  # no cached <v> -> openpyxl reads None
    )
    return _zip(
        {
            "[Content_Types].xml": f'<?xml version="1.0"?><Types xmlns="{_OOXML}package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument'
            '.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument'
            '.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument'
            '.spreadsheetml.sharedStrings+xml"/></Types>',
            "_rels/.rels": f'<?xml version="1.0"?><Relationships xmlns="{_OOXML}package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{rel}/officeDocument" Target="xl/workbook.xml"/></Relationships>',
            "xl/workbook.xml": f'<?xml version="1.0"?><workbook xmlns="{main}" xmlns:r="{rel}">'
            '<sheets><sheet name="Data" sheetId="1" r:id="rId1"/></sheets></workbook>',
            "xl/_rels/workbook.xml.rels": f'<?xml version="1.0"?><Relationships xmlns="{_OOXML}package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{rel}/worksheet" Target="worksheets/sheet1.xml"/>'
            f'<Relationship Id="rId2" Type="{rel}/sharedStrings" Target="sharedStrings.xml"/></Relationships>',
            "xl/sharedStrings.xml": f'<?xml version="1.0"?><sst xmlns="{main}" count="1" uniqueCount="1">'
            "<si><t>Item</t></si></sst>",
            "xl/worksheets/sheet1.xml": f'<?xml version="1.0"?><worksheet xmlns="{main}"><sheetData>{rows}</sheetData></worksheet>',
        }
    )


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


def test_render_lines_default_window_caps_long_file_with_footer():
    text = "\n".join(f"line{i}" for i in range(1, 501)) + "\n"
    out = _render_lines(text, None, None)
    body, _, footer = out.partition("\n\n-- ")
    assert body.splitlines()[0] == "1\tline1"
    assert body.splitlines()[-1] == f"{READ_DEFAULT_LINES}\tline{READ_DEFAULT_LINES}"
    assert f"line{READ_DEFAULT_LINES + 1}" not in body  # the window stops at the cap
    assert footer == f"showing lines 1-{READ_DEFAULT_LINES} of 500; pass start/end to read more --"


def test_trim_trailing_blank_rows_drops_only_trailing():
    rows = [["1", "2"], ["", ""], ["3", ""], ["", ""], ["", ""]]
    assert _trim_trailing_blank_rows(rows) == [["1", "2"], ["", ""], ["3", ""]]


def test_trim_trailing_blank_rows_all_blank_yields_empty():
    assert _trim_trailing_blank_rows([["", ""], ["", ""]]) == []


def _tool_turn(call_id: str, result: str) -> list:
    """One complete read_file tool exchange: an assistant call then its return."""
    return [
        ModelResponse(parts=[ToolCallPart(tool_name="read_file", args={"path": f"/{call_id}"}, tool_call_id=call_id)]),
        ModelRequest(parts=[ToolReturnPart(tool_name="read_file", content=result, tool_call_id=call_id)]),
    ]


def test_safe_cut_never_splits_a_tool_call_from_its_return():
    messages = [
        ModelRequest(parts=[UserPromptPart(content="hi")]),
        *_tool_turn("c1", "A"),  # indices 1 (call), 2 (return)
        *_tool_turn("c2", "B"),  # indices 3 (call), 4 (return)
    ]
    # A cut at index 2 would leave c1's call in the head but its return at the boundary;
    # it must snap back to before the call (index 1).
    assert _safe_cut(messages, 2) == 1
    # A cut between two complete pairs is already safe.
    assert _safe_cut(messages, 3) == 3


@pytest.mark.asyncio
async def test_compactor_summarizes_head_and_keeps_tail_orphan_free():
    compact = build_compactor(TestModel(custom_output_text="BRIEFING"), token_budget=1, keep_tail=2)
    messages = [
        ModelRequest(parts=[UserPromptPart(content="x" * 100)]),
        *_tool_turn("c1", "y" * 100),
        *_tool_turn("c2", "z" * 100),
    ]  # 5 messages, well over a 1-token budget

    out = await compact(messages)

    # Head collapses to one summary message; the last two messages are kept verbatim.
    assert len(out) == 3
    assert out[0].parts[0].content.startswith(SUMMARY_MARKER)
    assert "BRIEFING" in out[0].parts[0].content
    # Tool-pair-safe: every surviving return has its call present (c1's return was summarized
    # away together with c1's call, so nothing is orphaned).
    call_ids = {
        p.tool_call_id for m in out if isinstance(m, ModelResponse) for p in m.parts if isinstance(p, ToolCallPart)
    }
    return_ids = {
        p.tool_call_id for m in out if isinstance(m, ModelRequest) for p in m.parts if isinstance(p, ToolReturnPart)
    }
    assert return_ids <= call_ids


@pytest.mark.asyncio
async def test_compactor_leaves_history_under_budget_untouched():
    compact = build_compactor(TestModel(custom_output_text="unused"), token_budget=1_000_000, keep_tail=2)
    messages = [ModelRequest(parts=[UserPromptPart(content="small")]), *_tool_turn("c1", "ok")]
    assert await compact(messages) is messages


def _stub_models_get(monkeypatch, *, payload=None, exc=None):
    """Replace `model_info.httpx.get` with a stub returning `payload` (200) or raising `exc`."""

    def fake_get(url, headers=None, timeout=None):
        if exc is not None:
            raise exc
        return httpx.Response(200, json=payload, request=httpx.Request("GET", url))

    monkeypatch.setattr(model_info_mod.httpx, "get", fake_get)


def test_resolve_context_window_reads_omlx_max_model_len(monkeypatch):
    _stub_models_get(monkeypatch, payload={"data": [{"id": "Qwen3.6-27B-4bit", "max_model_len": 524288}]})
    assert resolve_context_window("http://x/v1", "Qwen3.6-27B-4bit", "k", fallback=32768) == (
        524288,
        "endpoint:max_model_len",
    )


def test_resolve_context_window_prefers_max_input_tokens(monkeypatch):
    # Both keys present: the Anthropic-shape key wins (first in the candidate list).
    _stub_models_get(monkeypatch, payload={"data": [{"id": "m", "max_input_tokens": 200000, "max_model_len": 999}]})
    assert resolve_context_window("http://x/v1", "m", "k", fallback=32768) == (200000, "endpoint:max_input_tokens")


def test_resolve_context_window_falls_back_on_null_field(monkeypatch):
    # omlx reports `max_model_len: null` for some models (e.g. MarkItDown).
    _stub_models_get(monkeypatch, payload={"data": [{"id": "MarkItDown", "max_model_len": None}]})
    assert resolve_context_window("http://x/v1", "MarkItDown", "k", fallback=32768) == (
        32768,
        "fallback:no-window-field",
    )


def test_resolve_context_window_falls_back_when_model_not_listed(monkeypatch):
    _stub_models_get(monkeypatch, payload={"data": [{"id": "other", "max_model_len": 8192}]})
    assert resolve_context_window("http://x/v1", "absent", "k", fallback=32768) == (32768, "fallback:model-not-listed")


def test_resolve_context_window_falls_back_on_connection_error(monkeypatch):
    _stub_models_get(monkeypatch, exc=httpx.ConnectError("refused"))
    assert resolve_context_window("http://x/v1", "m", "k", fallback=32768) == (32768, "fallback:ConnectError")


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
async def test_upload_writes_file_and_appears_in_tree(client_and_world):
    client, _ = client_and_world
    resp = await client.post("/api/vfs/upload", files={"file": ("hello.txt", b"hi there\n", "text/plain")})
    assert resp.status_code == 200
    body = resp.json()
    assert body["path"] == "/uploads/hello.txt"
    assert body["size"] == len(b"hi there\n")

    tree = (await client.get("/api/vfs/tree", params={"prefix": "/"})).json()
    assert "/uploads/hello.txt" in tree["paths"]

    got = (await client.get("/api/vfs/file", params={"path": "/uploads/hello.txt"})).json()
    assert got["content"] == "hi there\n"


@pytest.mark.asyncio
async def test_upload_stores_binary_as_opaque_bytes(client_and_world):
    client, world = client_and_world
    # A PNG signature: not valid UTF-8, and no registered extractor, so it stays opaque.
    blob = b"\x89PNG\r\n\x1a\n\x00\x01\x02\x03"
    resp = await client.post("/api/vfs/upload", files={"file": ("pixel.png", blob, "image/png")})
    assert resp.status_code == 200
    body = resp.json()
    assert body["size"] == len(blob)
    assert "derived_paths" not in body  # no extractor for .png -> no sidecar

    # Bytes round-trip exactly through the VFS regardless of type.
    session = Session(world.vfs, world.namespace_id, world.admin_id)
    assert await session.read("/uploads/pixel.png") == blob


@pytest.mark.asyncio
async def test_upload_pdf_writes_extracted_text_sidecar(client_and_world):
    client, _ = client_and_world
    pdf = _minimal_pdf("Hello VFS")
    resp = await client.post("/api/vfs/upload", files={"file": ("doc.pdf", pdf, "application/pdf")})
    assert resp.status_code == 200
    body = resp.json()
    assert "extract_error" not in body
    assert body["derived_paths"] == ["/uploads/doc.pdf.md"]

    # Both the original and the derived sidecar appear in the tree.
    tree = (await client.get("/api/vfs/tree", params={"prefix": "/"})).json()
    assert "/uploads/doc.pdf" in tree["paths"]
    assert "/uploads/doc.pdf.md" in tree["paths"]

    # The sidecar holds the extracted text — searchable/readable by the ordinary tools.
    sidecar = (await client.get("/api/vfs/file", params={"path": "/uploads/doc.pdf.md"})).json()
    assert "Hello VFS" in sidecar["content"]


@pytest.mark.asyncio
async def test_upload_docx_writes_text_sidecar(client_and_world):
    client, _ = client_and_world
    resp = await client.post(
        "/api/vfs/upload",
        files={"file": ("memo.docx", _docx("Hello from DOCX"), _DOCX_MIME)},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "extract_error" not in body
    assert body["derived_paths"] == ["/uploads/memo.docx.txt"]

    sidecar = (await client.get("/api/vfs/file", params={"path": "/uploads/memo.docx.txt"})).json()
    assert "Hello from DOCX" in sidecar["content"]


@pytest.mark.asyncio
async def test_upload_xlsx_writes_one_csv_sidecar_per_sheet_with_cached_values(client_and_world):
    client, _ = client_and_world
    resp = await client.post(
        "/api/vfs/upload",
        files={"file": ("book.xlsx", _xlsx_with_formulas(), _XLSX_MIME)},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "extract_error" not in body
    assert body["derived_paths"] == ["/uploads/book.xlsx.Data.csv"]

    csv_text = (await client.get("/api/vfs/file", params={"path": "/uploads/book.xlsx.Data.csv"})).json()["content"]
    assert "Item" in csv_text and "100" in csv_text
    assert "200" in csv_text  # =B1*2 cached result is surfaced (data_only=True)
    assert "101" not in csv_text  # =B1+1 has no cached value -> empty cell, never recomputed


def test_strip_ooxml_text_reads_docx_and_pptx_runs():
    assert _strip_ooxml_text(_docx("Hello from DOCX")) == "Hello from DOCX"
    assert _strip_ooxml_text(_pptx("Hello from PPTX")) == "Hello from PPTX"


@pytest.mark.asyncio
async def test_office_extractor_falls_back_to_stdlib_strip_without_soffice(monkeypatch):
    # No LibreOffice on PATH -> the stdlib zip+XML strip path, not liteparse.
    monkeypatch.setattr(extract_mod.shutil, "which", lambda _name: None)
    artifacts = await OfficeTextExtractor().extract(_docx("Fallback text"))
    assert [(a.suffix, a.text) for a in artifacts] == [(".txt", "Fallback text")]


def test_resolve_extractor_dispatches_by_extension_case_insensitively():
    assert isinstance(resolve_extractor(".pdf"), ContentExtractor)
    assert isinstance(resolve_extractor(".PDF"), ContentExtractor)  # extension is lowercased
    assert resolve_extractor(".png") is None  # no registered extractor
    assert resolve_extractor("") is None


@pytest.mark.asyncio
async def test_upload_rejects_oversized_file(client_and_world):
    client, _ = client_and_world
    oversized = b"\x00" * (MAX_UPLOAD_BYTES + 1)
    resp = await client.post("/api/vfs/upload", files={"file": ("big.bin", oversized, "application/octet-stream")})
    assert resp.status_code == 413


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
