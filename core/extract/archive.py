"""Text from archives, using only the standard library.

An archive is a container, not a document: what the index wants is the text
inside it, labelled with the entry it came from. The extraction is deliberately
bounded — entry count, per-entry bytes and total bytes — because a zip is also
the easiest way to hand an indexer a decompression bomb.
"""

from __future__ import annotations

import bz2
import gzip
import lzma
import tarfile
import zipfile
from pathlib import Path

MAX_ENTRIES = 400
MAX_ENTRY_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024

TEXTUAL = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".json",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".xml", ".html", ".htm",
    ".py", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".kt",
    ".c", ".h", ".cpp", ".hpp", ".cs", ".rb", ".php", ".sh", ".bash", ".ps1",
    ".sql", ".css", ".scss", ".lua", ".swift", ".tex", ".eml",
}


def _textual(name: str) -> bool:
    return Path(name).suffix.lower() in TEXTUAL


def _decode(data: bytes) -> str | None:
    if not data or b"\x00" in data[:4096]:
        return None
    for encoding in ("utf-8", "cp1251", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def _append(parts: list[str], label: str, text: str | None, budget: list[int]) -> None:
    if not text:
        return
    block = f"=== {label} ===\n{text}"
    if budget[0] - len(block) < 0:
        block = block[: max(0, budget[0])]
    parts.append(block)
    budget[0] -= len(block)


def extract_zip(path: str | Path, max_bytes: int = MAX_TOTAL_BYTES) -> str:
    parts: list[str] = []
    budget = [int(max_bytes)]
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist()[:MAX_ENTRIES]:
            if budget[0] <= 0:
                break
            if info.is_dir() or info.file_size > MAX_ENTRY_BYTES:
                continue
            if not _textual(info.filename):
                continue
            try:
                data = archive.read(info)
            except (OSError, RuntimeError, zipfile.BadZipFile):
                continue
            _append(parts, info.filename, _decode(data), budget)
    return "\n\n".join(parts)


def extract_tar(path: str | Path, max_bytes: int = MAX_TOTAL_BYTES) -> str:
    parts: list[str] = []
    budget = [int(max_bytes)]
    try:
        archive = tarfile.open(path, "r:*")
    except (OSError, tarfile.TarError):
        return ""
    with archive:
        for info in archive.getmembers()[:MAX_ENTRIES]:
            if budget[0] <= 0:
                break
            if not info.isreg() or info.size > MAX_ENTRY_BYTES:
                continue
            if not _textual(info.name):
                continue
            handle = archive.extractfile(info)
            if handle is None:
                continue
            try:
                data = handle.read(MAX_ENTRY_BYTES)
            except OSError:
                continue
            _append(parts, info.name, _decode(data), budget)
    return "\n\n".join(parts)


def extract_single(path: str | Path, max_bytes: int = MAX_TOTAL_BYTES) -> str:
    """A `.gz`/`.bz2`/`.xz` that is not a tar: one stream, decoded as text."""
    suffix = Path(path).suffix.lower()
    opener = {".gz": gzip.open, ".bz2": bz2.open, ".xz": lzma.open}.get(suffix)
    if opener is None:
        return ""
    try:
        with opener(path, "rb") as handle:
            data = handle.read(min(int(max_bytes), MAX_ENTRY_BYTES))
    except (OSError, EOFError, lzma.LZMAError):
        return ""
    text = _decode(data)
    if text is None:
        return ""
    name = Path(path).stem
    return f"=== {name} ===\n{text}"


def extract_archive(path: str | Path, max_bytes: int = MAX_TOTAL_BYTES) -> str:
    suffix = Path(path).suffix.lower()
    if suffix == ".zip":
        return extract_zip(path, max_bytes)
    if suffix in (".tar", ".tgz", ".tbz2", ".txz", ".tar"):
        return extract_tar(path, max_bytes)
    # `.tgz` and friends are tar files whose *name* says so; tarfile's "r:*"
    # mode also opens a bare `.tar` with compression, so try it before treating
    # the stream as a single document.
    if zipfile.is_zipfile(path):
        return extract_zip(path, max_bytes)
    if tarfile.is_tarfile(path):
        return extract_tar(path, max_bytes)
    return extract_single(path, max_bytes)
