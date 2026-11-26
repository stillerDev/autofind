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

from .ignore import IGNORE_FILES, IgnoreSet
from .privacy import match_reason, sensitive_dirs

TEXT_EXT = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".json",
    ".jsonl", ".ndjson", ".jsonc",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".tex",
    ".xml", ".svg", ".plist", ".diff", ".patch", ".nfo", ".org", ".adoc",
    ".asciidoc", ".textile", ".rmd", ".ipynb", ".properties",
    # Config/manifest/build files that are text but used to land in "other".
    ".bat", ".cmd", ".sln", ".vcxproj", ".filters", ".props", ".targets",
    ".recipe", ".manifest", ".editorconfig", ".gitignore", ".gitattributes",
    ".dockerfile", ".nix", ".hcl", ".tfstate", ".lock", ".sum", ".mod",
    ".typed", ".tex-json", ".vdf", ".usda", ".usdc", ".mtlx", ".osl", ".oso",
    ".dat", ".f90", ".f95", ".for", ".inl", ".glslfx", ".hlsli", ".fx",
    ".rpy", ".qss", ".tslint", ".prettierrc", ".babelrc", ".env",
    # Templates, schemas and dotfiles: all plain text despite the odd suffix.
    ".json5", ".jsonc", ".cjs", ".mjs", ".cts", ".mts", ".hbs", ".ejs",
    ".jade", ".pug", ".haml", ".slim", ".erb", ".liquid", ".mustache",
    ".njk", ".twig", ".xsd", ".xsl", ".xslt", ".dtd", ".rdf", ".owl",
    ".n3", ".ttl", ".sparql", ".rq", ".avsc", ".avdl", ".thrift",
    ".capnp", ".tfvars", ".nomad", ".eslintrc", ".stylelintrc",
    ".flake8", ".pylintrc", ".coveragerc", ".dockerignore", ".npmignore",
    ".gitmodules", ".mailmap", ".bashrc", ".zshrc", ".profile", ".vimrc",
    ".inputrc", ".htaccess", ".properties", ".srt", ".vtt", ".ass", ".ssa",
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
    ".f", ".f90", ".f95", ".for", ".inl", ".glslfx", ".hlsli", ".fx",
    ".usda", ".rpy", ".qml", ".gradle", ".mk",
}
DOC_EXT = {".pdf", ".docx", ".pptx", ".xlsx", ".epub", ".odt", ".ods", ".odp", ".rtf"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
# Archives and mail are containers: the text lives in their entries, and the
# extractor labels each one so a result can say where inside the file it found
# the match. `.7z` needs py7zr and stays out; Outlook `.msg` is mail here and
# is read through the optional `extract-msg` package (see core/extract/msgoutlook).
ARCHIVE_EXT = {".zip", ".tar", ".tgz", ".tbz2", ".txz", ".gz", ".bz2", ".xz"}
EMAIL_EXT = {".eml", ".mbox", ".msg"}
# Sound (and video) have no useful text body. They are the one family we do
# NOT sniff: an mp3/wav can contain ASCII metadata, but treating that as a
# document produces garbage results.
AUDIO_EXT = {
    ".mp3", ".wav", ".flac", ".ogg", ".oga", ".opus", ".m4a", ".aac",
    ".wma", ".aiff", ".aif", ".alac", ".mid", ".midi", ".amr", ".ape",
    ".mp2", ".mka", ".m4b", ".caf", ".au", ".snd", ".voc", ".8svx",
}
VIDEO_EXT = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v",
    ".mpg", ".mpeg", ".3gp", ".ts", ".m2ts", ".vob", ".ogv", ".mk3d",
}
MEDIA_EXT = AUDIO_EXT | VIDEO_EXT

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


def _is_probably_text(path: str | Path) -> bool:
    """Last-resort classification for unknown extensions and extensionless files.

    Reading 4 KiB is cheap compared with never indexing the contents. A NUL in
    the first block or a high control-character ratio means a real binary
    (dll/exe/pyc/media), which stays 'other' instead of filling the index with
    mojibake.
    """
    try:
        with open(path, "rb") as handle:
            head = handle.read(4096)
    except OSError:
        return False
    if not head or b"\x00" in head:
        return False
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):
        return True
    from .extract.plain import extract_plain_bytes

    text = extract_plain_bytes(head)
    if not text:
        return False
    sample = text[:1024]
    control = sum(1 for ch in sample if ord(ch) < 32 and ch not in "\n\r\t")
    return control / len(sample) < 0.04


def kind_for(path: str | Path) -> str:
    ext = Path(path).suffix.lower()
    # Known kinds come first: `.ts` is both TypeScript and an MPEG transport
    # stream, and the source-code meaning is the one the index has always used.
    known = KIND_BY_EXT.get(ext)
    if known is not None:
        return known
    if ext in MEDIA_EXT:
        return "other"  # audio/video: the one family we never text-sniff
    return "text" if _is_probably_text(path) else "other"


def sha256_file(path: str | Path, chunk: int = 1 << 20) -> bytes:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.digest()


def dedupe_roots(roots: Iterable[str | Path]) -> list[str]:
    """Normalise the root list: one string per real folder, no repeats.

    The list comes from the panel's textarea, so it can hold the same folder
    twice or a folder and its subfolder side by side; walking both walks the
    same files twice. Exact repeats are dropped (Windows paths are
    case-insensitive) and a root an earlier root already covers goes with
    them, so the first root decides what the scan covers.
    """
    resolved: list[str] = []
    for root in roots:
        text = str(root).strip()
        if not text:
            continue
        try:
            path = Path(text).expanduser()
            resolved.append(str(path.resolve()))
        except OSError:
            resolved.append(text)
    out: list[str] = []
    for root in resolved:
        key = os.path.normcase(root)
        if any(key == os.path.normcase(other) for other in out):
            continue
        if any(
            key.startswith(os.path.normcase(other).rstrip(os.sep) + os.sep)
            for other in out
        ):
            continue
        out.append(root)
    return out


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
                # Dotfiles such as .env, .bashrc and .dockerignore are text and
                # indexed by design; only the ignore manifests themselves are
                # skipped here (their rules are applied by IgnoreSet above).
                if name in IGNORE_FILES:
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
                # Same classification as the single-file path: an unknown
                # extension is sniffed here too, instead of being labelled
                # "other" by the walk and "text" when passed in directly.
                kind = kind_for(path)
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
