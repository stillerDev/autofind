"""Plain text and source code extraction."""

from __future__ import annotations

from pathlib import Path

ENCODINGS = ("utf-8-sig", "utf-8", "cp1251", "koi8-r", "latin-1")
BINARY_MARKERS = (b"\x00", b"\xff\xd8\xff", b"PK\x03\x04", b"%PDF")


def extract_plain(path: str | Path, max_bytes: int = 20 * 1024 * 1024) -> str:
    """Read a text file, guessing the encoding. Returns '' for binaries."""
    with open(path, "rb") as handle:
        data = handle.read(max_bytes)
    return extract_plain_bytes(data)


def extract_plain_bytes(data: bytes) -> str:
    """Decode already-read bytes, guessing the encoding. Returns '' for binaries.

    Lets the indexer hash and extract from ONE read of the file instead of
    paying two open/read round-trips per file."""
    if not data:
        return ""
    if b"\x00" in data[:4096]:
        return ""
    for encoding in ENCODINGS:
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        if _looks_like_text(text):
            return text
    return data.decode("utf-8", errors="replace")


def _looks_like_text(text: str) -> bool:
    sample = text[:4096]
    if not sample:
        return True
    control = sum(1 for ch in sample if ord(ch) < 32 and ch not in "\n\r\t")
    return control / len(sample) < 0.02
