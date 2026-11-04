"""DOCX extraction.

python-docx is optional: the fallback reads word/document.xml straight out of
the package, which covers paragraphs and tables in body order and needs no
third-party dependency.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def extract_docx(path: str | Path) -> str:
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
        text = paragraph.text.strip()
        if text:
            parts.append(text)
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def _extract_via_zip(path: str | Path) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            xml = archive.read("word/document.xml")
    except (KeyError, zipfile.BadZipFile, OSError):
        return ""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return ""
    body = root.find(f"{W}body")
    if body is None:
        return ""

    lines: list[str] = []
    _walk(body, lines)
    return "\n".join(lines)


def _walk(element: ET.Element, lines: list[str]) -> None:
    for child in element:
        if child.tag == f"{W}p":
            text = "".join(node.text or "" for node in child.iter(f"{W}t")).strip()
            if text:
                lines.append(text)
        elif child.tag == f"{W}tbl":
            for row in child.findall(f"{W}tr"):
                cells = []
                for cell in row.findall(f"{W}tc"):
                    cell_text = " ".join(
                        "".join(node.text or "" for node in paragraph.iter(f"{W}t"))
                        for paragraph in cell.findall(f"{W}p")
                    ).strip()
                    cells.append(cell_text)
                if any(cells):
                    lines.append(" | ".join(cells))
        else:
            _walk(child, lines)
