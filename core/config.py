"""Configuration and default locations.

The index lives in a single SQLite file inside the user profile, never in the
scanned tree: scanning a folder must not modify it.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .privacy import DEFAULT_CATEGORIES

APP_NAME = "autofind"

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
    override = os.environ.get("AUTOFIND_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".autofind"


# The first-run setup is a property of the installation, not of the webview's
# localStorage: clearing a cache must not replay the welcome screen on a machine
# that has been using the app for months, and a fresh profile must not skip it.
ONBOARDING_FILE = "onboarding.json"


def onboarding_done(home: Path) -> bool:
    """True once the welcome/setup screen has been completed or skipped."""
    try:
        data = json.loads((home / ONBOARDING_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return bool(isinstance(data, dict) and data.get("done"))


def mark_onboarding_done(home: Path) -> None:
    try:
        home.mkdir(parents=True, exist_ok=True)
        (home / ONBOARDING_FILE).write_text(
            json.dumps({"done": True}, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


def reset_onboarding(home: Path) -> None:
    try:
        (home / ONBOARDING_FILE).unlink()
    except OSError:
        pass


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
    # Which groups of the privacy guard are on. `privacy` stays the master
    # switch (and the CLI's --no-privacy path); this is the finer choice the
    # panel exposes. Empty means "the filter is on but watches nothing".
    privacy_categories: frozenset[str] = DEFAULT_CATEGORIES
    allow_sensitive: bool = False
    watch_interval: float = 2.0
    watch_settle: float = 1.0
    # 0 = auto (min(4, cpu cores)); 1 = the old single-process pipeline.
    extract_workers: int = 0
    # "pack" (memory-mapped side file, default), "sqlite-vec" (vec0 tables,
    # needs the extension, ~7x slower per query here) or "blob" (the old
    # in-process numpy matrix, 2.6 s cold start).
    vector_backend: str = "pack"

    def __post_init__(self) -> None:
        # Preferences are a file, not a dataclass argument: the panel edits
        # them while the server is running and the next launch must remember.
        prefs = read_privacy_prefs(self.home)
        if prefs is None:
            return
        if isinstance(prefs.get("enabled"), bool):
            self.privacy = prefs["enabled"]
        categories = prefs.get("categories")
        if isinstance(categories, list):
            self.privacy_categories = frozenset(
                name for name in categories if name in DEFAULT_CATEGORIES
            )

    @property
    def db_path(self) -> Path:
        return self.home / "index.db"

    @property
    def model_dir(self) -> Path:
        return self.home / "models"

    def ensure_home(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)


# The panel's privacy choices live in the config directory next to the index.
# They are deliberately not in the database: a rebuilt index should not reset
# the guard, and a backup of ~/.autofind should carry it.
PRIVACY_FILE = "privacy.json"


def read_privacy_prefs(home: Path) -> dict | None:
    try:
        data = json.loads((home / PRIVACY_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def save_privacy_prefs(config: "IndexConfig") -> None:
    try:
        config.home.mkdir(parents=True, exist_ok=True)
        (config.home / PRIVACY_FILE).write_text(
            json.dumps(
                {
                    "enabled": bool(config.privacy),
                    "categories": sorted(config.privacy_categories),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass
