"""Pluggable content extractors — pull text out of binary file types at the upload boundary.

A ports-and-adapters seam. `ContentExtractor` is the port; adapters (the
liteparse-backed PDF/Office extractor, the openpyxl-backed spreadsheet extractor)
are resolved by file extension through a lazy registry that mirrors the core
execution-provider registry in `vfs.execution.registry`. Extraction runs at
ingest, not inside `vfs.write`, so the VFS contract is untouched: the original
bytes are stored as-is and each extracted artifact lands in a sidecar file the
agent reads and searches with the ordinary tools.

An adapter may emit more than one artifact — a spreadsheet becomes one CSV
sidecar per sheet — so `extract` returns a list.

Dispatch is by lowercased file extension. The extension is authoritative on
upload (the client hands us a named file) and is the only key that separates the
ZIP-container Office formats (`.xlsx`/`.docx`/`.pptx` share magic bytes), so
content sniffing would not help. Add a row to `_EXTRACTORS` to register a type.

If a future adapter needs the MIME type (not just the extension), prefer the
`content-types` package (https://mkennedy.codes/docs/content-types/) over the
stdlib `mimetypes` module: it ships far more comprehensive, current mappings
(360+ formats including `.parquet`, `.ipynb`, `.toml`, `.yaml`) and avoids
`mimetypes`' stale entries (e.g. it returns `application/xml` for `.xml`, not the
deprecated `text/xml`).
"""

from __future__ import annotations

import asyncio
import csv
from dataclasses import dataclass
import importlib
import importlib.util
import io
import re
import shutil
from typing import Protocol, runtime_checkable
import zipfile

from defusedxml.ElementTree import fromstring as xml_fromstring


@dataclass(frozen=True)
class ExtractedFile:
    """One text artifact produced from a binary document.

    `suffix` is appended to the original VFS path to form the sidecar path:
    `/uploads/foo.pdf` + `.md` -> `/uploads/foo.pdf.md`;
    `/uploads/book.xlsx` + `.Sheet1.csv` -> `/uploads/book.xlsx.Sheet1.csv`.
    """

    suffix: str
    text: str


@runtime_checkable
class ContentExtractor(Protocol):
    """Port: turn a binary document's bytes into one or more text artifacts."""

    def suffixes(self) -> tuple[str, ...]:
        """Return the lowercased extensions this adapter handles, e.g. ``(".pdf",)``."""
        ...

    async def extract(self, content: bytes) -> list[ExtractedFile]:
        """Extract text artifacts from `content`; raise on unparsable input."""
        ...


async def _liteparse_text(content: bytes) -> str:
    """Extract a document's text via liteparse (OCR off), off the event loop."""
    import liteparse

    # liteparse.parse is a synchronous, CPU-bound native call; offload it. Office
    # documents are handled by converting to PDF via LibreOffice first, so this path
    # is only taken when `soffice` is present (see OfficeTextExtractor).
    result = await asyncio.to_thread(lambda: liteparse.LiteParse(ocr_enabled=False, quiet=True).parse(content))
    return result.text


class PdfExtractor:
    """Adapter: extract a PDF's embedded text layer via liteparse (OCR disabled)."""

    def suffixes(self) -> tuple[str, ...]:
        """Return the extensions this adapter handles."""
        return (".pdf",)

    async def extract(self, content: bytes) -> list[ExtractedFile]:
        """Extract the PDF's text layer into a single ``.md`` sidecar."""
        return [ExtractedFile(".md", await _liteparse_text(content))]


def _localname(tag: str) -> str:
    """Strip the XML namespace from a tag, leaving the local name (``{ns}t`` -> ``t``)."""
    return tag.rsplit("}", 1)[-1]


def _strip_ooxml_text(content: bytes) -> str:
    """Extract plain text from a docx/pptx package with the stdlib only.

    Reads the main body part(s) — `word/document.xml` for Word, each
    `ppt/slides/slideN.xml` for PowerPoint — and joins the text runs (`<w:t>`/`<a:t>`,
    local name ``t``) within each paragraph (local name ``p``), one paragraph per line.
    Both formats share those local names, so one pass handles both. This is a text-only
    fallback for when LibreOffice is unavailable; it does not reconstruct tables.
    """
    paragraphs: list[str] = []
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = set(archive.namelist())
        if "word/document.xml" in names:
            targets = ["word/document.xml"]
        else:
            targets = sorted(n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n))
        for name in targets:
            root = xml_fromstring(archive.read(name))
            for para in root.iter():
                if _localname(para.tag) != "p":
                    continue
                runs = [node.text for node in para.iter() if _localname(node.tag) == "t" and node.text]
                if runs:
                    paragraphs.append("".join(runs))
    return "\n".join(paragraphs)


class OfficeTextExtractor:
    """Adapter: extract Word/PowerPoint text.

    Uses liteparse (which converts via LibreOffice, preserving structure such as
    tables) when the `soffice` binary is available; otherwise falls back to a
    stdlib zip+XML text strip. Emits a single ``.txt`` sidecar.
    """

    def suffixes(self) -> tuple[str, ...]:
        """Return the extensions this adapter handles."""
        return (".docx", ".pptx")

    async def extract(self, content: bytes) -> list[ExtractedFile]:
        """Extract document text into a single ``.txt`` sidecar."""
        if shutil.which("soffice") or shutil.which("libreoffice"):
            text = await _liteparse_text(content)
        else:
            text = await asyncio.to_thread(_strip_ooxml_text, content)
        return [ExtractedFile(".txt", text)]


def _safe_segment(name: str) -> str:
    """Reduce a sheet name to a path-safe segment (``Q1 Sales`` -> ``Q1_Sales``)."""
    segment = re.sub(r"[^A-Za-z0-9._-]", "_", name).strip("._")
    return segment or "sheet"


def _trim_trailing_blank_rows(rows: list[list]) -> list[list]:
    """Drop wholly-empty rows from the end of `rows`; interior blank rows are kept.

    openpyxl reports a sheet's stored dimension, which often trails past the real data with
    blank rows; trimming them keeps the CSV sidecar from ending in empty lines.
    """
    while rows and all(cell == "" for cell in rows[-1]):
        rows.pop()
    return rows


class XlsxExtractor:
    """Adapter: convert a spreadsheet to one CSV sidecar per sheet via openpyxl.

    Reads with ``data_only=True``, so cells show the value cached by the application
    that last saved the file. openpyxl has no formula engine, so a formula whose
    result was never cached (e.g. a programmatically generated workbook) reads as an
    empty cell rather than being recomputed.

    Read-time recalculation (Option 2) would require a calc engine — routing through
    LibreOffice when `soffice` is present, mirroring OfficeTextExtractor — and is
    deliberately not done here.
    """

    def suffixes(self) -> tuple[str, ...]:
        """Return the extensions this adapter handles."""
        return (".xlsx", ".xlsm")

    async def extract(self, content: bytes) -> list[ExtractedFile]:
        """Convert each worksheet to a CSV sidecar named for the sheet."""
        return await asyncio.to_thread(self._to_csvs, content)

    def _to_csvs(self, content: bytes) -> list[ExtractedFile]:
        import openpyxl

        workbook = openpyxl.load_workbook(io.BytesIO(content), data_only=True, read_only=True)
        try:
            outputs: list[ExtractedFile] = []
            used: set[str] = set()
            for sheet in workbook.worksheets:
                segment = _safe_segment(sheet.title)
                if segment in used:  # keep collided sheet names distinct
                    segment = f"{segment}_{len(used)}"
                used.add(segment)
                rows = [["" if value is None else value for value in row] for row in sheet.iter_rows(values_only=True)]
                buffer = io.StringIO()
                writer = csv.writer(buffer, lineterminator="\n")
                for row in _trim_trailing_blank_rows(rows):
                    writer.writerow(row)
                outputs.append(ExtractedFile(f".{segment}.csv", buffer.getvalue()))
            return outputs
        finally:
            workbook.close()


#: Lowercased suffix -> (driver_module guard | None, adapter_module, class_name). The
#: driver is the importable dependency the adapter needs; None means the adapter's hard
#: requirement is stdlib-only (Office text falls back to a stdlib strip). The adapter is
#: loaded lazily so a missing dependency yields an actionable error, not an opaque
#: ModuleNotFoundError.
_EXTRACTORS: dict[str, tuple[str | None, str, str]] = {
    ".pdf": ("liteparse", "demo.backend.extract", "PdfExtractor"),
    ".docx": (None, "demo.backend.extract", "OfficeTextExtractor"),
    ".pptx": (None, "demo.backend.extract", "OfficeTextExtractor"),
    ".xlsx": ("openpyxl", "demo.backend.extract", "XlsxExtractor"),
    ".xlsm": ("openpyxl", "demo.backend.extract", "XlsxExtractor"),
}


def resolve_extractor(suffix: str) -> ContentExtractor | None:
    """Return an extractor for `suffix`, or None when no type is registered for it.

    Raises ImportError with an install hint when a type is registered but its
    backing dependency is absent — matching the core provider registries.
    """
    spec = _EXTRACTORS.get(suffix.lower())
    if spec is None:
        return None
    driver, adapter_module, class_name = spec
    if driver is not None and importlib.util.find_spec(driver) is None:
        raise ImportError(
            f"Extractor for {suffix!r} requires the {driver!r} package, which is not installed. "
            f"Install the demo's dev group: uv sync --group dev"
        )
    module = importlib.import_module(adapter_module)
    return getattr(module, class_name)()
