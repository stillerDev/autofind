"""PDF extraction.

pypdf is used when installed. Without it a minimal fallback walks Flate
streams and pulls strings out of text operators: it handles simple text PDFs
and returns '' for scanned or CID-font documents, where OCR is the honest
answer anyway.
"""

from __future__ import annotations

import re
import zlib
from pathlib import Path

STREAM_RE = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.S)
PIECE_RE = re.compile(rb"\((?:[^()\\]|\\.)*\)|<[0-9A-Fa-f\s]*>|T\*|Td|TD|Tj|TJ|ET")
ESCAPES = {
    b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f",
    b"(": b"(", b")": b")", b"\\": b"\\",
}


def extract_pdf(path: str | Path) -> str:
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError:
        PdfReader = None

    if PdfReader is not None:
        try:
            reader = PdfReader(str(path))
            text = "\n".join((page.extract_text() or "") for page in reader.pages)
            if text.strip():
                return text
        except Exception:
            pass

    return _minimal_text(Path(path).read_bytes())


def _minimal_text(data: bytes) -> str:
    blocks: list[str] = []
    for match in STREAM_RE.finditer(data):
        content = _inflate(match.group(1))
        if b"Tj" not in content and b"TJ" not in content:
            continue
        block = _content_text(content)
        if block:
            blocks.append(block)
    return "\n".join(blocks).strip()


def _inflate(raw: bytes) -> bytes:
    for candidate in (raw, raw.strip(), raw.lstrip(b"\r\n")):
        try:
            return zlib.decompress(candidate)
        except zlib.error:
            continue
    try:
        return zlib.decompressobj().decompress(raw)
    except zlib.error:
        return raw


def _content_text(content: bytes) -> str:
    out: list[str] = []
    for piece in PIECE_RE.findall(content):
        if piece in (b"Td", b"TD", b"T*", b"ET"):
            out.append("\n")
        elif piece.startswith(b"("):
            out.append(_decode_literal(piece[1:-1]))
        elif piece.startswith(b"<"):
            hex_body = re.sub(rb"\s", b"", piece[1:-1])
            if len(hex_body) % 2:
                hex_body += b"0"
            try:
                out.append(bytes.fromhex(hex_body.decode("ascii")).decode("latin-1"))
            except ValueError:
                continue
    text = "".join(out)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def _decode_literal(raw: bytes) -> str:
    result = bytearray()
    index = 0
    while index < len(raw):
        char = raw[index:index + 1]
        if char != b"\\":
            result += char
            index += 1
            continue
        nxt = raw[index + 1:index + 2]
        if nxt in ESCAPES:
            result += ESCAPES[nxt]
            index += 2
        elif nxt.isdigit():
            octal = re.match(rb"[0-7]{1,3}", raw[index + 1:index + 4])
            result.append(int(octal.group(0), 8) & 0xFF if octal else 32)
            index += 1 + (len(octal.group(0)) if octal else 1)
        else:
            index += 2
    return result.decode("utf-8", errors="replace")
