"""Configuration and default locations.

The index lives in a single SQLite file inside the user profile, never in the
scanned tree: scanning a folder must not modify it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .privacy import DEFAULT_CATEGORIES

APP_NAME = "autofind"
_LOGGER = logging.getLogger(__name__)

# Speed-first default. The hashing embedder needs neither a 96 MB model mmap
# nor a 17 MB tokenizer parse: a warm server stays around 65 MB RSS instead of
# 335 MB, and a one-shot CLI starts in ~280 ms instead of ~930 ms. The
# higher-quality static-e5 channel is still available with
# `AUTOFIND_EMBEDDER=auto` (server) or `--embedder auto` (CLI).
os.environ.setdefault("AUTOFIND_EMBEDDER", "hashing")

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
    # Files above this size contribute no full text: their name,
    # hash and visual vector still index. 0 disables the rule.
    content_skip_over_mb: int = 0
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
            # A typo used to be silently dropped, quietly disabling protection
            # for that category. Keep the historical behaviour (unknown names
            # are not protected) but make the mistake visible.
            unknown = [name for name in categories if name not in DEFAULT_CATEGORIES]
            if unknown:
                _LOGGER.warning(
                    "privacy.json: ignoring unknown categories: %s",
                    ", ".join(sorted(str(name) for name in unknown)),
                )
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


# Per-root indexing rules and global ignore patterns live next to the index,
# not in the scanned tree. They are user preferences (like privacy.json), so a
# rebuilt index does not reset them and a backup of the home carries them.
RULES_FILE = "index-rules.json"
MAX_RULE_PATTERNS = 200
MAX_PATTERN_CHARS = 256
MAX_RULE_ROOTS = 200

_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")


def normalise_patterns(values, *, field_name: str = "patterns") -> list[str]:
    """Validate UI-supplied ignore globs. Bad entries are rejected, not dropped.

    Patterns are glob fragments interpreted by `core.ignore`: `*.tmp`,
    `**/node_modules`, `.git`, or `/build` (anchored to the indexed root).
    Absolute drive/UNC paths, traversal segments and control characters would
    let a page aim a rule outside the indexed tree, so they are refused.
    """
    if not isinstance(values, list):
        raise ValueError(f"{field_name} must be a list")
    result: list[str] = []
    for raw in values:
        if not isinstance(raw, str):
            raise ValueError(f"{field_name} must contain only strings")
        pattern = raw.strip().replace("\\", "/")
        if not pattern:
            continue
        if len(pattern) > MAX_PATTERN_CHARS:
            raise ValueError(f"{field_name} entries are limited to {MAX_PATTERN_CHARS} characters")
        if any(char in pattern for char in ("\x00", "\r", "\n")):
            raise ValueError(f"{field_name} entries cannot contain control characters")
        if pattern.startswith("//") or _DRIVE_PREFIX.match(pattern):
            raise ValueError(f"{field_name} must be relative to the indexed root")
        if ".." in pattern.split("/"):
            raise ValueError(f"{field_name} cannot contain '..' traversal")
        if pattern not in result:
            result.append(pattern)
    if len(result) > MAX_RULE_PATTERNS:
        raise ValueError(f"at most {MAX_RULE_PATTERNS} {field_name} are allowed")
    return result


def normalise_dir_names(values, *, field_name: str = "excluded_dirs") -> list[str]:
    """Directory *names* for the scanner's exact-match `excluded_dirs` set."""
    names = normalise_patterns(values, field_name=field_name)
    for name in names:
        if "/" in name or "*" in name or "?" in name:
            raise ValueError(f"{field_name} must be plain directory names, not globs")
    return names


def empty_rules() -> dict:
    return {"roots": {}, "exclusions": []}


def read_index_rules(home: Path) -> dict:
    """Read index-rules.json, tolerating a missing or damaged file.

    Reads are defensive: a half-written file must never take the search UI
    down. Writes go through `save_index_rules`, which validates strictly.
    """
    try:
        data = json.loads((Path(home) / RULES_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty_rules()
    if not isinstance(data, dict):
        return empty_rules()
    roots: dict[str, dict] = {}
    raw_roots = data.get("roots")
    if isinstance(raw_roots, dict):
        for path, rule in list(raw_roots.items())[:MAX_RULE_ROOTS]:
            if not isinstance(path, str) or not isinstance(rule, dict):
                continue
            key = str(Path(path).expanduser())
            try:
                exclusions = normalise_patterns(
                    rule.get("exclusions") or [], field_name="root exclusions"
                )
                dirs = normalise_dir_names(rule.get("excluded_dirs") or [])
            except ValueError:
                continue
            roots[key] = {
                "enabled": rule.get("enabled") is not False,
                "exclusions": exclusions,
                "excluded_dirs": dirs,
                "max_file_mb": _safe_int(rule.get("max_file_mb"), 0, 0, 1024),
            }
    try:
        exclusions = normalise_patterns(data.get("exclusions") or [])
    except ValueError:
        exclusions = []
    return {"roots": roots, "exclusions": exclusions}


def _safe_int(value, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(value or default)))
    except (TypeError, ValueError):
        return default


def save_index_rules(home: Path, payload) -> dict:
    """Validate and persist per-root rules plus global exclusion patterns."""
    if not isinstance(payload, dict):
        raise ValueError("rules must be an object")
    raw_roots = payload.get("roots", {})
    if not isinstance(raw_roots, dict) or len(raw_roots) > MAX_RULE_ROOTS:
        raise ValueError(f"at most {MAX_RULE_ROOTS} roots are allowed")
    roots: dict[str, dict] = {}
    for raw_path, rule in raw_roots.items():
        if not isinstance(raw_path, str) or not isinstance(rule, dict):
            raise ValueError("each root must map to a rule object")
        path = str(Path(raw_path.strip()).expanduser())
        if not path or len(path) > 1024:
            raise ValueError("invalid root path")
        raw_mb = rule.get("max_file_mb", 0)
        if isinstance(raw_mb, bool) or not isinstance(raw_mb, (int, str)):
            raise ValueError("max_file_mb must be an integer between 0 and 1024")
        max_mb = _safe_int(raw_mb, 0, 0, 1024)
        try:
            parsed_mb = int(raw_mb)
        except (TypeError, ValueError):
            raise ValueError("max_file_mb must be an integer between 0 and 1024") from None
        if parsed_mb != max_mb:
            raise ValueError("max_file_mb must be an integer between 0 and 1024")
        roots[path] = {
            "enabled": bool(rule.get("enabled", True)),
            "exclusions": normalise_patterns(rule.get("exclusions") or [],
                                             field_name="root exclusions"),
            "excluded_dirs": normalise_dir_names(rule.get("excluded_dirs") or []),
            "max_file_mb": max_mb,
        }
    data = {
        "roots": roots,
        "exclusions": normalise_patterns(payload.get("exclusions") or []),
    }
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    target = home / RULES_FILE
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)
    return data


def portable_status(home: Path | None = None, executable: Path | None = None) -> dict:
    """Is the index home a `data` folder sitting next to the executable?

    A portable install carries its index with the app (`<exe dir>/data`), so
    the panel must not imply the data lives in the user profile. The answer is
    based on the *effective* home: an AUTOFIND_HOME override pointing anywhere
    else is not portable, even when an EXE happens to sit beside a data dir.
    """
    override = os.environ.get("AUTOFIND_HOME")
    effective = Path(home if home is not None else default_home()).expanduser().resolve()
    exe = Path(executable or sys.executable).expanduser().resolve()
    folder = exe.parent
    candidates = {folder.resolve(), (folder / "data").resolve(), (folder / "Data").resolve()}
    portable = bool(override) and effective in candidates
    return {
        "portable": portable,
        "home": str(effective),
        "executable": str(exe),
        "data_dir": str(folder / "data"),
        "env_override": bool(override),
    }
