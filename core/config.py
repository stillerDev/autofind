"""Configuration and default locations.

The index lives in a single SQLite file inside the user profile, never in the
scanned tree: scanning a folder must not modify it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

APP_NAME = "semfind"

EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "env",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "target",
        "dist",
        "build",
        ".idea",
        ".vscode",
        "$RECYCLE.BIN",
        "System Volume Information",
        "AppData",
        ".cache",
        ".workbuddy-ai",
    }
)


def default_home() -> Path:
    override = os.environ.get("SEMFIND_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".semfind"


@dataclass(slots=True)
class IndexConfig:
    """Tunables for scanning, chunking and storage."""

    home: Path = field(default_factory=default_home)
    chunk_chars: int = 1200
    chunk_overlap: int = 200
    max_file_bytes: int = 20 * 1024 * 1024
    min_file_bytes: int = 32
    batch_size: int = 32
    thumb_size: int = 320
    ocr_languages: str = "rus+eng"
    ocr_engine: str | None = None
    ocr_enabled: bool = True
    # Second OCR pass with a CJK-capable engine when the first one clearly could
    # not read the image. Costs a second pass only on screenshots that failed.
    ocr_cjk: bool = True
    ocr_cjk_model: str | None = None      # e.g. a japan/korean PP-OCR rec model
    ocr_cjk_keys: str | None = None       # its dictionary
    excluded_dirs: frozenset[str] = EXCLUDED_DIRS
    privacy: bool = True
    allow_sensitive: bool = False
    watch_interval: float = 2.0
    watch_settle: float = 1.0
    # 0 = auto (min(4, cpu cores)); 1 = the old single-process pipeline.
    extract_workers: int = 0
    # "pack" (memory-mapped side file, default), "sqlite-vec" (vec0 tables,
    # needs the extension, ~7x slower per query here) or "blob" (the old
    # in-process numpy matrix, 2.6 s cold start).
    vector_backend: str = "pack"

    @property
    def db_path(self) -> Path:
        return self.home / "index.db"

    @property
    def model_dir(self) -> Path:
        return self.home / "models"

    def ensure_home(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
