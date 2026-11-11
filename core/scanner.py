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

from .ignore import IgnoreSet
from .privacy import match_reason, sensitive_dirs

TEXT_EXT = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".json",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".tex",
    ".xml", ".svg", ".plist", ".diff", ".patch", ".nfo", ".org", ".adoc",
    ".asciidoc", ".textile", ".rmd", ".ipynb", ".properties",
}
CODE_EXT = {
    ".py", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".kt",
    ".c", ".h", ".cpp", ".hpp", ".cs", ".rb", ".php", ".sh", ".bash", ".ps1",
    ".sql", ".html", ".css", ".scss", ".vue", ".svelte", ".lua", ".swift",
    ".pyi", ".pyx", ".pxd", ".cxx", ".cc", ".hh", ".hxx", ".m", ".mm",
    ".r", ".jl", ".dart", ".ex", ".exs", ".erl", ".hrl", ".clj", ".cljs",
    ".scala", ".groovy", ".pl", ".pm", ".zig", ".nim", ".v", ".sv", ".vhd",
    ".vhdl", ".asm", ".s", ".fs", ".fsx", ".vb", ".pas", ".lisp", ".el",
    ".scm", ".rkt", ".hs", ".ml", ".mli", ".elm", ".cr", ".d", ".sol", ".ino",
    ".gd", ".glsl", ".hlsl", ".frag", ".vert", ".comp", ".geom", ".tesc",
    ".tese", ".metal", ".wgsl", ".cu", ".cuh", ".cmake", ".mk", ".gradle",
    ".proto", ".graphql", ".tf", ".hcl", ".shader",
}
DOC_EXT = {".pdf", ".docx", ".pptx", ".xlsx", ".epub", ".odt", ".ods", ".odp", ".rtf"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
# Archives and mail are containers: the text lives in their entries, and the
# extractor labels each one so a result can say where inside the file it found
# the match. `.7z` and Outlook `.msg` need third-party libraries and stay out.
ARCHIVE_EXT = {".zip", ".tar", ".tgz", ".tbz2", ".txz", ".gz", ".bz2", ".xz"}
EMAIL_EXT = {".eml", ".mbox"}

KIND_BY_EXT = {
    **{ext: "text" for ext in TEXT_EXT},
    **{ext: "code" for ext in CODE_EXT},
    ".pdf": "pdf",
    ".docx": "docx",
    ".pptx": "pptx",
    ".xlsx": "xlsx",
    ".epub": "epub",
    # OpenDocument is one XML schema behind three extensions, so one kind.
    ".odt": "odt",
    ".ods": "odt",
    ".odp": "odt",
    ".rtf": "rtf",
    **{ext: "image" for ext in IMAGE_EXT},
    **{ext: "archive" for ext in ARCHIVE_EXT},
    **{ext: "email" for ext in EMAIL_EXT},
}

SUPPORTED_KINDS = frozenset({
    "text", "code", "pdf", "docx", "pptx", "xlsx", "epub", "odt", "rtf",
    "image", "archive", "email", "other",
})


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
    respect_ignore: bool = True,
    categories: frozenset[str] | set[str] | None = None,
) -> Iterator[FileEntry]:
    """Yield supported files under every root, skipping noise directories.

    With `privacy` on, sensitive locations are dropped before they are opened and
    appended to `skipped` as (path, reason) so the caller can report the count.

    With `respect_ignore` on, `.gitignore` / `.cursorignore` / `.autofindignore`
    are honoured: an ignored directory is pruned from the walk (git semantics —
    nothing inside can be re-included) and ignored files never reach the index.

    The walk uses `os.scandir` directly: on Windows the directory listing
    already carries size, mtime and file/dir type, so per-file `stat()`,
    `is_file()` and `Path.resolve()` round-trips disappear — the scan of a
    100k-file tree drops from ~20 s to ~4 s. Path resolution is cached per
    directory instead of per file, which keeps the same absolute paths for
    every regular file (only a symlinked *file* would previously have resolved
    to its target; those never matched a supported extension anyway).
    """
    seen: set[str] = set()
    blocked = sensitive_dirs(categories) if privacy else frozenset()
    for root in roots:
        base = Path(root).expanduser()
        if base.is_file():
            entry = _entry_for(base, max_bytes, min_bytes)
            if entry and entry.path not in seen and _passes_privacy(
                entry.path, privacy, skipped, categories
            ):
                seen.add(entry.path)
                yield entry
            continue
        if not base.is_dir():
            continue
        ignore = IgnoreSet()
        stack = [str(base)]
        while stack:
            dirpath = stack.pop()
            if respect_ignore:
                ignore.add_directory(dirpath)
            try:
                listing = list(os.scandir(dirpath))
            except OSError:
                continue
            dir_resolved: str | None = None  # resolved once per directory
            for entry in listing:
                name = entry.name
                if name.startswith("."):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if name in excluded_dirs:
                        continue
                    subdir = os.path.join(dirpath, name)
                    if privacy and name.lower() in blocked:
                        if skipped is not None:
                            skipped.append((subdir, f"directory '{name.lower()}'"))
                        continue
                    if respect_ignore and ignore.is_ignored(subdir, True):
                        continue
                    stack.append(subdir)
                    continue
                path = os.path.join(dirpath, name)
                if respect_ignore and ignore.is_ignored(path, False):
                    continue
                suffix = os.path.splitext(name)[1].lower()
                kind = KIND_BY_EXT.get(suffix)
                if kind is None or kind not in SUPPORTED_KINDS:
                    # Unknown or binary format: still record the name so it is
                    # findable by filename. No text is read for these.
                    kind = "other"
                try:
                    stat = entry.stat()
                except OSError:
                    continue
                if not entry.is_file():
                    continue
                if kind != "other" and (stat.st_size < min_bytes or stat.st_size > max_bytes):
                    continue
                if dir_resolved is None:
                    dir_resolved = str(Path(dirpath).resolve())
                full = os.path.join(dir_resolved, name)
                if full in seen:
                    continue
                if not _passes_privacy(full, privacy, skipped, categories):
                    continue
                seen.add(full)
                yield FileEntry(full, stat.st_size, stat.st_mtime, kind)


def _passes_privacy(
    path: str,
    privacy: bool,
    skipped: list[tuple[str, str]] | None,
    categories: frozenset[str] | set[str] | None = None,
) -> bool:
    if not privacy:
        return True
    reason = match_reason(path, categories)
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
