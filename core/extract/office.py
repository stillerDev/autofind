"""Office and e-book extraction with nothing but the standard library.

Every format here is a zip of XML (OOXML, OpenDocument, EPUB) or a plain markup
stream (RTF), so `zipfile` + `ElementTree` covers them. That keeps the installer
small, which is the whole point of the project's bundle budget.

Zip bombs: these formats are compressed containers, and a few hundred kilobytes
can legally expand to gigabytes — a hostile file must never wedge an indexing
run. Every archive member is therefore read through a byte-capped window, and
XML parsing is charged to per-archive event and shared-string budgets (the same
numbers Kensaku 1.0.17 fixed on after its own fuzzing round). When a budget
fires, the extractor returns the text read so far: for search, a truncated
document beats not indexing the file at all, so the truncation is handled here
and never surfaces as an error.
"""

from __future__ import annotations

import codecs
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

A_T = "{http://schemas.openxmlformats.org/drawingml/2006/main}t"
S_SI = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}si"
S_T = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t"
S_V = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}v"
S_ROW = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}row"
S_C = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}c"
TEXT_P = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}p"
TEXT_H = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}h"
TEXT_SPAN = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}span"

MAX_SHEET_ROWS = 20_000
MAX_SLIDES = 500
MAX_EPUB_PARTS = 400

# ---------------------------------------------------------- zip-bomb budgets

# Decompressed bytes read from ONE archive member. A small zip can expand to
# gigabytes; the event and string budgets bound the work we keep, but a single
# giant text node would still be buffered whole by the parser. Truncating the
# window turns a decompression bomb into a normal truncated document.
MAX_XML_INPUT_BYTES = 64 * 1024 * 1024

# Decompressed bytes charged to ONE archive across all of its members. The
# per-member window alone lets 400 EPUB chapters each claim their full 64 MB.
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024

# XML end-events processed for one archive. The output limits alone do not
# stop a crafted file: it can hold millions of empty elements that never add a
# byte to the text but still cost parser time. This is the budget that bounds
# the work.
MAX_XML_EVENTS = 2_000_000

# Shared strings kept while reading an XLSX: only the first MAX_SHARED_STRINGS
# strings (up to MAX_SHARED_STRING_BYTES of text in total) are ever referenced
# by cells we still have room to keep, so holding more is pure memory risk.
MAX_SHARED_STRINGS = 20_000
MAX_SHARED_STRING_BYTES = 8 * 1024 * 1024


class BudgetExhausted(Exception):
    """A budget fired. Extractors catch this and keep what they have."""


class Budget:
    """Per-archive accounting, shared by every member of one container."""

    __slots__ = ("bytes", "events")

    def __init__(self) -> None:
        self.bytes = 0
        self.events = 0


class _BoundedReader:
    """File-like view of one decompressed member that stops at `limit` bytes
    and charges every byte to the archive budget.

    It stops rather than raising on the window limit, so the parser sees a
    truncated stream and fails with an ordinary ParseError we catch — the bomb
    never materialises in memory. The archive-wide budget raises
    `BudgetExhausted`, which the caller keeps partial text through.
    """

    def __init__(self, stream, budget: Budget, limit: int) -> None:
        self._stream = stream
        self._budget = budget
        self._limit = limit
        self._read = 0

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = self._limit - self._read
        size = min(size, self._limit - self._read)
        if size <= 0:
            return b""
        data = self._stream.read(size)
        self._read += len(data)
        self._budget.bytes += len(data)
        if self._budget.bytes > MAX_ARCHIVE_BYTES:
            raise BudgetExhausted("archive decompression budget exhausted")
        return data

    def close(self) -> None:
        self._stream.close()


def bounded_read(archive: zipfile.ZipFile, name: str, budget: Budget) -> bytes:
    """One member's bytes under the window and archive budgets. Markup readers
    (EPUB chapters) use this instead of `archive.read`, which decompresses
    without a limit."""
    with archive.open(name) as raw:
        # MAX_XML_INPUT_BYTES is read here, not captured at import, so tests
        # can shrink the window.
        return _BoundedReader(raw, budget, MAX_XML_INPUT_BYTES).read(MAX_XML_INPUT_BYTES)


def xml_events(archive: zipfile.ZipFile, name: str, budget: Budget, events=("end",)):
    """(event, element) pairs from one member, under the input window and the
    per-archive event budget. The generator raises `BudgetExhausted` past the
    event cap; a truncated stream ends in `ET.ParseError`, which callers catch
    the same way."""
    with archive.open(name) as raw:
        reader = _BoundedReader(raw, budget, MAX_XML_INPUT_BYTES)
        for event, elem in ET.iterparse(reader, events=events):
            budget.events += 1
            if budget.events > MAX_XML_EVENTS:
                raise BudgetExhausted("xml event budget exhausted")
            yield event, elem


# Keep the exception tuple in one place: a dead member must cost a slide or a
# chapter, never the whole file. ValueError rides along because a malformed
# shared-string index (`<v>not-a-number</v>`) raises it from `int()` mid-parse.
_DEATHS = (ET.ParseError, BudgetExhausted, zipfile.BadZipFile, KeyError, OSError, ValueError)


def extract_pptx(path: str | Path) -> str:
    """Slide text in slide order. Speaker notes are skipped on purpose: they are
    rarely what you are searching for and double the noise."""
    parts: list[str] = []
    budget = Budget()
    with zipfile.ZipFile(path) as archive:
        slides = sorted(
            (name for name in archive.namelist()
             if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)),
            key=lambda name: int(re.search(r"(\d+)", name.rsplit("/", 1)[-1]).group(1)),
        )[:MAX_SLIDES]
        for name in slides:
            texts: list[str] = []
            try:
                for _, node in xml_events(archive, name, budget):
                    if node.tag == A_T and node.text and node.text.strip():
                        texts.append(node.text)
                    node.clear()  # leaf nodes: clearing keeps the tree flat
            except _DEATHS:
                pass  # one dead or truncated slide keeps the text read so far
            if texts:
                parts.append(" ".join(texts))
    return "\n".join(parts)


def extract_xlsx(path: str | Path) -> str:
    """Cell values sheet by sheet, one line per row, ` | ` between cells."""
    lines: list[str] = []
    budget = Budget()
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            # The table cells we keep are capped, so capping the string table
            # to match is safe: past the caps the cells read empty anyway.
            kept_bytes = 0
            try:
                for _, item in xml_events(archive, "xl/sharedStrings.xml", budget):
                    if item.tag != S_SI:
                        continue
                    text = "".join(node.text or "" for node in item.iter(S_T))
                    if (
                        len(shared) < MAX_SHARED_STRINGS
                        and kept_bytes + len(text) <= MAX_SHARED_STRING_BYTES
                    ):
                        shared.append(text)
                        kept_bytes += len(text)
                    item.clear()
            except _DEATHS:
                pass  # rows below read whatever strings made it through

        sheets = sorted(
            (name for name in names
             if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)),
            key=lambda name: int(re.search(r"(\d+)", name.rsplit("/", 1)[-1]).group(1)),
        )
        for name in sheets:
            # The row cap is per sheet: one 20k-row sheet must not silence every
            # sheet that follows it in the workbook.
            rows_on_sheet = 0
            try:
                for _, row in xml_events(archive, name, budget):
                    if row.tag != S_ROW:
                        continue
                    if rows_on_sheet >= MAX_SHEET_ROWS:
                        break
                    cells: list[str] = []
                    for cell in row.iter(S_C):
                        kind = cell.get("t")
                        value = cell.find(S_V)
                        if kind == "s" and value is not None and value.text:
                            index = int(value.text)
                            cells.append(shared[index] if 0 <= index < len(shared) else "")
                        elif kind == "inlineStr":
                            cells.append("".join(node.text or "" for node in cell.iter(S_T)))
                        elif value is not None and value.text:
                            cells.append(value.text)
                    if any(cell.strip() for cell in cells):
                        lines.append(" | ".join(cells))
                        rows_on_sheet += 1
                    row.clear()  # rows are the bulk of a sheet: drop as we go
            except _DEATHS:
                continue  # truncated sheet: keep the rows read so far
    return "\n".join(lines)


def _strip_markup(blob: bytes) -> str:
    text = blob.decode("utf-8", errors="replace")
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"'))
    return re.sub(r"[ \t]+", " ", text)


def extract_epub(path: str | Path) -> str:
    """Chapter text in spine order when the OPF is readable, else file order."""
    parts: list[str] = []
    budget = Budget()
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        order: list[str] = []
        opf = next((name for name in names if name.lower().endswith(".opf")), None)
        if opf:
            # One streaming pass collects manifest items and spine refs; the
            # spine is rebuilt from them after the element budget ends the
            # parse, so a giant OPF costs one bounded pass.
            manifest: dict[str, str] = {}
            idrefs: list[str] = []
            try:
                for _, node in xml_events(archive, opf, budget):
                    tag = node.tag.rsplit("}", 1)[-1]
                    if tag == "item" and node.get("id") and node.get("href"):
                        manifest[node.get("id")] = node.get("href")
                    elif tag == "itemref" and node.get("idref"):
                        idrefs.append(node.get("idref"))
                    node.clear()
            except _DEATHS:
                pass
            base = opf.rsplit("/", 1)[0] + "/" if "/" in opf else ""
            order = [base + manifest[ref] for ref in idrefs if ref in manifest]
        if not order:
            order = [name for name in names
                     if name.lower().endswith((".xhtml", ".html", ".htm"))]
        for name in order[:MAX_EPUB_PARTS]:
            if name not in names:
                continue
            try:
                # The window caps one chapter; a bomb chapter becomes a
                # truncated one instead of a decompression stall.
                parts.append(_strip_markup(bounded_read(archive, name, budget)))
            except BudgetExhausted:
                break  # the archive budget is spent: no further chapter fits
            except _DEATHS:
                # One broken chapter is not the end of the book: the comment
                # above promises to keep reading the ones after it.
                continue
    return "\n".join(part.strip() for part in parts if part.strip())


def extract_opendocument(path: str | Path) -> str:
    """ODT/ODS/ODP: `content.xml` paragraphs and headings."""
    lines: list[str] = []
    budget = Budget()
    with zipfile.ZipFile(path) as archive:
        if "content.xml" not in archive.namelist():
            return ""
        try:
            for _, node in xml_events(archive, "content.xml", budget):
                if node.tag in (TEXT_P, TEXT_H):
                    text = "".join(node.itertext()).strip()
                    if text:
                        lines.append(text)
                node.clear()  # paragraphs and headings are what we keep
        except _DEATHS:
            pass  # truncated content: the paragraphs read so far stand
    return "\n".join(lines)


_RTF_SKIP = re.compile(r"\{\\\*.*?\}", re.S)
_RTF_CONTROL = re.compile(r"\\[a-zA-Z]+-?\d* ?")
_RTF_HEX = re.compile(r"\\'([0-9a-fA-F]{2})")


def extract_rtf(path: str | Path) -> str:
    """RTF is plain markup: drop destinations, control words and braces.

    RTF is a disk file, not a compressed container, so there is no expansion
    gap to close; the scanner's size caps already bound the read.
    """
    raw = Path(path).read_bytes().decode("latin-1", errors="replace")
    # RTF declares the codepage of \'xx escapes with \ansicpgNNNN. cp1251 stays
    # the default for files that omit it; an unknown code is ignored rather
    # than turning the whole document into an exception.
    codepage = 1251
    declared = re.search(r"\\ansicpg(\d+)", raw)
    if declared:
        candidate = f"cp{declared.group(1)}"
        try:
            codecs.lookup(candidate)
            codepage = int(declared.group(1))
        except LookupError:
            pass
    codec = f"cp{codepage}"
    raw = _RTF_SKIP.sub(" ", raw)
    raw = _RTF_HEX.sub(
        lambda m: bytes([int(m.group(1), 16)]).decode(codec, errors="replace"), raw
    )
    raw = _RTF_CONTROL.sub(" ", raw)
    raw = raw.replace("\\{", "{").replace("\\}", "}").replace("\\\\", "\\")
    raw = raw.replace("{", " ").replace("}", " ")
    return re.sub(r"[ \t]+", " ", raw).strip()
