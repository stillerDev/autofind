"""Office and e-book extraction with nothing but the standard library.

Every format here is a zip of XML (OOXML, OpenDocument, EPUB) or a plain markup
stream (RTF), so `zipfile` + `ElementTree` covers them. That keeps the installer
small, which is the whole point of the project's bundle budget.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

A_T = "{http://schemas.openxmlformats.org/drawingml/2006/main}t"
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


def extract_pptx(path: str | Path) -> str:
    """Slide text in slide order. Speaker notes are skipped on purpose: they are
    rarely what you are searching for and double the noise."""
    parts: list[str] = []
    with zipfile.ZipFile(path) as archive:
        slides = sorted(
            (name for name in archive.namelist()
             if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)),
            key=lambda name: int(re.search(r"(\d+)", name.rsplit("/", 1)[-1]).group(1)),
        )[:MAX_SLIDES]
        for name in slides:
            root = ET.fromstring(archive.read(name))
            texts = [node.text for node in root.iter(A_T) if node.text and node.text.strip()]
            if texts:
                parts.append(" ".join(texts))
    return "\n".join(parts)


def extract_xlsx(path: str | Path) -> str:
    """Cell values sheet by sheet, one line per row, ` | ` between cells."""
    lines: list[str] = []
    with zipfile.ZipFile(path) as archive:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in root:
                shared.append("".join(node.text or "" for node in item.iter(S_T)))

        sheets = sorted(
            (name for name in archive.namelist()
             if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)),
            key=lambda name: int(re.search(r"(\d+)", name.rsplit("/", 1)[-1]).group(1)),
        )
        for name in sheets:
            root = ET.fromstring(archive.read(name))
            for row in root.iter(S_ROW):
                if len(lines) >= MAX_SHEET_ROWS:
                    break
                cells: list[str] = []
                for cell in row.iter(S_C):
                    kind = cell.get("t")
                    value = cell.find(S_V)
                    if kind == "s" and value is not None and value.text:
                        index = int(value.text)
                        cells.append(shared[index] if index < len(shared) else "")
                    elif kind == "inlineStr":
                        cells.append("".join(node.text or "" for node in cell.iter(S_T)))
                    elif value is not None and value.text:
                        cells.append(value.text)
                if any(cell.strip() for cell in cells):
                    lines.append(" | ".join(cells))
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
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        order: list[str] = []
        opf = next((name for name in names if name.lower().endswith(".opf")), None)
        if opf:
            try:
                root = ET.fromstring(archive.read(opf))
                manifest = {
                    item.get("id"): item.get("href")
                    for item in root.iter()
                    if item.tag.endswith("item") and item.get("id") and item.get("href")
                }
                base = opf.rsplit("/", 1)[0] + "/" if "/" in opf else ""
                for item in root.iter():
                    if not item.tag.endswith("itemref"):
                        continue
                    href = manifest.get(item.get("idref"))
                    if href:
                        order.append(base + href)
            except ET.ParseError:
                order = []
        if not order:
            order = [name for name in names
                     if name.lower().endswith((".xhtml", ".html", ".htm"))]
        parts = [_strip_markup(archive.read(name)) for name in order[:MAX_EPUB_PARTS]
                 if name in names]
    return "\n".join(part.strip() for part in parts if part.strip())


def extract_opendocument(path: str | Path) -> str:
    """ODT/ODS/ODP: `content.xml` paragraphs and headings."""
    with zipfile.ZipFile(path) as archive:
        if "content.xml" not in archive.namelist():
            return ""
        root = ET.fromstring(archive.read("content.xml"))
    lines: list[str] = []
    for node in root.iter():
        if node.tag in (TEXT_P, TEXT_H):
            text = "".join(node.itertext()).strip()
            if text:
                lines.append(text)
    return "\n".join(lines)


_RTF_SKIP = re.compile(r"\{\\\*.*?\}", re.S)
_RTF_CONTROL = re.compile(r"\\[a-zA-Z]+-?\d* ?")
_RTF_HEX = re.compile(r"\\'([0-9a-fA-F]{2})")


def extract_rtf(path: str | Path) -> str:
    """RTF is plain markup: drop destinations, control words and braces."""
    raw = Path(path).read_bytes().decode("latin-1", errors="replace")
    raw = _RTF_SKIP.sub(" ", raw)
    raw = _RTF_HEX.sub(lambda m: bytes([int(m.group(1), 16)]).decode("cp1251", errors="replace"), raw)
    raw = _RTF_CONTROL.sub(" ", raw)
    raw = raw.replace("\\{", "{").replace("\\}", "}").replace("\\\\", "\\")
    raw = raw.replace("{", " ").replace("}", " ")
    return re.sub(r"[ \t]+", " ", raw).strip()
