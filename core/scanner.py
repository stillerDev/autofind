"""Filesystem scanner.

Walks the configured roots, classifies files by extension and computes a
sha256 only when size or mtime changed, so a repeated run over an unchanged
tree costs almost nothing.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from .privacy import SENSITIVE_DIRS, match_reason

TEXT_EXT = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".json",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".tex",
}
CODE_EXT = {
    ".py", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".kt",
    ".c", ".h", ".cpp", ".hpp", ".cs", ".rb", ".php", ".sh", ".bash", ".ps1",
    ".sql", ".html", ".css", ".scss", ".vue", ".svelte", ".lua", ".swift",
}
DOC_EXT = {".pdf", ".docx"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}

KIND_BY_EXT = {
    **{ext: "text" for ext in TEXT_EXT},
    **{ext: "code" for ext in CODE_EXT},
    **{ext: "pdf" for ext in DOC_EXT if ext == ".pdf"},
    **{ext: "docx" for ext in DOC_EXT if ext == ".docx"},
    **{ext: "image" for ext in IMAGE_EXT},
}

SUPPORTED_KINDS = frozenset({"text", "code", "pdf", "docx", "image"})


@dataclass(slots=True)
class FileEntry:
    path: str
    size: int
    mtime: float
    kind: str


def kind_for(path: str | Path) -> str:
    ext = Path(path).suffix.lower()
    return KIND_BY_EXT.get(ext, "other")


def sha256_file(path: str | Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def iter_entries(
    roots: Iterable[str | Path],
    excluded_dirs: frozenset[str] = frozenset(),
    max_bytes: int = 20 * 1024 * 1024,
    min_bytes: int = 32,
    privacy: bool = True,
    skipped: list[tuple[str, str]] | None = None,
) -> Iterator[FileEntry]:
    """Yield supported files under every root, skipping noise directories.

    With `privacy` on, sensitive locations are dropped before they are opened and
    appended to `skipped` as (path, reason) so the caller can report the count.
    """
    seen: set[str] = set()
    for root in roots:
        base = Path(root).expanduser()
        if base.is_file():
            entry = _entry_for(base, max_bytes, min_bytes)
            if entry and entry.path not in seen and _passes_privacy(entry.path, privacy, skipped):
                seen.add(entry.path)
                yield entry
            continue
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            kept = []
            for name in dirnames:
                if name in excluded_dirs or name.startswith("."):
                    continue
                if privacy and name.lower() in SENSITIVE_DIRS:
                    if skipped is not None:
                        skipped.append((str(Path(dirpath) / name), f"directory '{name.lower()}'"))
                    continue
                kept.append(name)
            dirnames[:] = kept
            for name in filenames:
                if name.startswith("."):
                    continue
                entry = _entry_for(Path(dirpath) / name, max_bytes, min_bytes)
                if entry and entry.path not in seen and _passes_privacy(entry.path, privacy, skipped):
                    seen.add(entry.path)
                    yield entry


def _passes_privacy(path: str, privacy: bool, skipped: list[tuple[str, str]] | None) -> bool:
    if not privacy:
        return True
    reason = match_reason(path)
    if reason is None:
        return True
    if skipped is not None:
        skipped.append((path, reason))
    return False


def _entry_for(path: Path, max_bytes: int, min_bytes: int) -> FileEntry | None:
    kind = kind_for(path)
    if kind not in SUPPORTED_KINDS:
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    if not path.is_file():
        return None
    if stat.st_size < min_bytes or stat.st_size > max_bytes:
        return None
    return FileEntry(str(path.resolve()), stat.st_size, stat.st_mtime, kind)
