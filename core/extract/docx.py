"""DOCX extraction.

python-docx is optional: the fallback reads word/document.xml straight out of
the package, which covers paragraphs and tables in body order and needs no
third-party dependency.

Both paths are budgeted. python-docx parses the whole package into memory and
collects every paragraph, so a decompression bomb spends the process; the
zip fallback reads through the same capped window and event budget as the
other office formats (see office.py for the rationale). Past a cap the text
simply stops: a truncated document is still searchable.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from .office import Budget, BudgetExhausted, xml_events

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

# Paragraph and table lines kept from one document. Kensaku bounds this by its
# XML event budget; here the line count is the direct knob (python-docx has no
# event stream to charge).
MAX_DOCX_LINES = 100_000

# A package past either threshold goes straight to the bounded zip reader.
# python-docx materialises the whole OPC tree (and every paragraph) before our
# line cap applies, so a compressed bomb would spend the process first.
MAX_DOCX_XML_BYTES = 8 * 1024 * 1024
MAX_DOCX_ENTRIES = 500


def _needs_bounded_path(path: str | Path) -> bool:
    """Cheap zip-directory check: is this package too big for python-docx?"""
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_DOCX_ENTRIES:
                return True
            for info in infos:
                if info.filename == "word/document.xml":
                    return info.file_size > MAX_DOCX_XML_BYTES
    except (zipfile.BadZipFile, OSError):
        # Not a readable OPC package: python-docx gets its chance and falls
        # through to the zip reader itself when it cannot open the file.
        return False
    return False


def extract_docx(path: str | Path) -> str:
    if _needs_bounded_path(path):
        return _extract_via_zip(path)
    try:
        import docx  # type: ignore
    except ImportError:
        return _extract_via_zip(path)

    try:
        document = docx.Document(str(path))
    except Exception:
        # python-docx insists on a full OPC package; a minimal or damaged file
        # that still has word/document.xml is readable by the fallback.
        return _extract_via_zip(path)

    parts: list[str] = []
    for paragraph in document.paragraphs:
        if len(parts) >= MAX_DOCX_LINES:
            break
        text = paragraph.text.strip()
        if text:
            parts.append(text)
    for table in document.tables:
        if len(parts) >= MAX_DOCX_LINES:
            break
        for row in table.rows:
            if len(parts) >= MAX_DOCX_LINES:
                break
            cells = [cell.text.strip() for cell in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def _extract_via_zip(path: str | Path) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            if "word/document.xml" not in archive.namelist():
                return ""
            budget = Budget()
            lines: list[str] = []
            # Depth of open tables. A paragraph inside a table belongs to the
            # table's ` | `-joined row, not to the body; a nested table is
            # covered when its outer table ends.
            tbl_depth = 0
            try:
                for event, node in xml_events(
                    archive, "word/document.xml", budget, events=("start", "end")
                ):
                    if node.tag == f"{W}tbl":
                        if event == "start":
                            tbl_depth += 1
                        else:
                            tbl_depth -= 1
                            if tbl_depth == 0:
                                _table(node, lines)
                                node.clear()
                    elif node.tag == f"{W}p" and event == "end" and tbl_depth == 0:
                        text = "".join(
                            node.text or "" for node in node.iter(f"{W}t")
                        ).strip()
                        if text:
                            lines.append(text)
                        node.clear()  # body paragraphs are the bulk: drop as we go
            except (ET.ParseError, BudgetExhausted, zipfile.BadZipFile, KeyError, OSError):
                # A truncated stream ends in a ParseError with the read text
                # already collected; a fired budget raises explicitly.
                pass
            return "\n".join(lines)
    except (zipfile.BadZipFile, OSError, KeyError):
        return ""


def _table(table: ET.Element, lines: list[str]) -> None:
    for row in table.findall(f"{W}tr"):
        cells = []
        for cell in row.findall(f"{W}tc"):
            cell_text = " ".join(
                "".join(node.text or "" for node in paragraph.iter(f"{W}t"))
                for paragraph in cell.findall(f"{W}p")
            ).strip()
            cells.append(cell_text)
        if any(cells):
            lines.append(" | ".join(cells))
