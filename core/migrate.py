"""Bring a kensaku install over in one command.

The reason people stay on a search app is never the search — it is the twenty
pinned files, the four saved queries and the ten dictionary words they typed
in over a year. Asking them to retype those is asking them not to switch. So
the switch has to be one command that leaves nothing behind:

    autofind import kensaku

It reads kensaku's own config, wherever that config happens to be, and copies
pins, saved searches and dictionary entries into the same SQLite file the index
lives in. Nothing is written until `--write`, and a pin whose file is not in the
index is reported, not silently dropped.

kensaku is a Tauri app, so its store is one of a handful of known places and
one of two shapes: a JSON file or a SQLite database. Rather than depend on a
schema we cannot pin down (it is their private format and may change), the
reader walks whatever it finds and recognises three kinds of data by name:
paths under keys about pins/favourites, queries under keys about saved
searches, and pairs under keys about aliases. That survives a schema change
without a code change, and it never invents an entry out of nothing.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .store import Store

# Where a Tauri app keeps its data. `kensaku` is the bundle identifier's last
# segment on all three platforms, so the directory name is the same; only the
# parent differs. All of them are checked, cheaply, and only existing ones are
# read.
APP_NAMES = ("kensaku", "Kensaku", "com.kensaku.app", "app.kensaku")


def candidate_dirs() -> list[Path]:
    home = Path.home()
    roots: list[Path] = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        roots.append(Path(appdata))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(Path(local))
    roots += [
        home / "AppData" / "Roaming",
        home / "AppData" / "Local",
        home / ".config",
        home / ".local" / "share",
        home / "Library" / "Application Support",
    ]
    seen: list[Path] = []
    for root in roots:
        for name in APP_NAMES:
            path = root / name
            if path not in seen:
                seen.append(path)
    return seen


# A config file, not a cache: sizes are bounded so pointing this at a 200 MB
# Chromium cache cannot stall the import.
MAX_BYTES = 4 * 1024 * 1024


@dataclass
class Source:
    """What one config file contained, before it is matched to the index."""

    path: Path
    kind: str  # "json" | "sqlite"
    pins: list[str] = field(default_factory=list)
    saved: list[dict] = field(default_factory=list)
    aliases: dict[str, str] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not (self.pins or self.saved or self.aliases)

    def as_dict(self) -> dict:
        return {
            "path": str(self.path),
            "kind": self.kind,
            "pins": len(self.pins),
            "saved": len(self.saved),
            "aliases": len(self.aliases),
        }


def _inside(directory: Path) -> list[Path]:
    """The config-shaped files in one folder, and one level down.

    One level down because a Tauri app often nests its store by version or by
    profile, and a person pointing at the parent folder means "here, somewhere".
    """
    out: list[Path] = []
    names = ("config.json", "settings.json", "store.json", "kensaku.json",
             "data.json", "index.db", "kensaku.db", "store.db", "db.sqlite3")
    for name in names:
        out.append(directory / name)
    try:
        children = sorted(directory.iterdir())
    except OSError:
        return out
    for child in children:
        if child.is_dir():
            for name in ("config.json", "settings.json", "kensaku.json", "index.db"):
                out.append(child / name)
    return out


def find_sources(extra: Iterable[str | Path] = ()) -> list[Source]:
    """Every readable kensaku config, plus anything passed on the command line."""
    found: list[Source] = []
    seen: set[Path] = set()
    candidates: list[Path] = []
    for directory in candidate_dirs():
        if directory.is_dir():
            candidates.extend(_inside(directory))
    for item in extra:
        path = Path(item)
        # A path on the command line may be the file itself or the folder that
        # holds it, because "point it at the config" and "point it at the app's
        # data folder" are both things a person says.
        candidates.extend(_inside(path) if path.is_dir() else [path])

    for path in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        try:
            source = read(resolved)
        except Exception:
            continue  # an unreadable config is not an error worth stopping for
        if not source.empty:
            found.append(source)
    return found


def read(path: Path) -> Source:
    if path.stat().st_size > MAX_BYTES:
        return Source(path, "skipped")
    with path.open("rb") as handle:
        head = handle.read(16)
    if head.startswith(b"SQLite format 3"):
        return _read_sqlite(path)
    return _read_json(path)


# ---------------------------------------------------------------- JSON


def _read_json(path: Path) -> Source:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError):
        return Source(path, "json")
    source = Source(path, "json")
    _walk(data, source, depth=0)
    return source


PIN_WORDS = ("pin", "favorite", "favourite", "bookmark", "starred")
SAVED_WORDS = ("saved", "savedsearch", "saved_search", "preset")
ALIAS_WORDS = ("alias", "aliases", "dictionary", "synonym", "expand")


def _key_hits(key: str, words: tuple[str, ...]) -> bool:
    lowered = key.lower().replace("-", "").replace("_", "")
    return any(word in lowered for word in words)


def _walk(node: Any, source: Source, depth: int) -> None:
    """Recognise pins, saved queries and aliases anywhere in the tree."""
    if depth > 8:
        return
    if isinstance(node, dict):
        for key, value in node.items():
            if not isinstance(key, str):
                continue
            if _key_hits(key, PIN_WORDS):
                source.pins.extend(_paths(value))
            elif _key_hits(key, ALIAS_WORDS):
                source.aliases.update(_pairs(value))
            elif _key_hits(key, SAVED_WORDS):
                source.saved.extend(_queries(value))
            else:
                _walk(value, source, depth + 1)
    elif isinstance(node, list):
        for item in node:
            _walk(item, source, depth + 1)


def _paths(value: Any) -> list[str]:
    out: list[str] = []
    items = value if isinstance(value, list) else [value]
    for item in items:
        if isinstance(item, str) and _looks_like_path(item):
            out.append(item)
        elif isinstance(item, dict):
            for key in ("path", "file", "file_path", "filePath", "abs_path"):
                candidate = item.get(key)
                if isinstance(candidate, str) and _looks_like_path(candidate):
                    out.append(candidate)
                    break
    return out


def _looks_like_path(text: str) -> bool:
    if len(text) < 3 or "\n" in text:
        return False
    return ("\\" in text or "/" in text) and "." in text.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]


def _pairs(value: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    if isinstance(value, dict):
        for alias, expand in value.items():
            if isinstance(expand, str) and expand.strip():
                out[str(alias).strip().lower()] = " ".join(expand.split())
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                alias = item.get("alias") or item.get("from") or item.get("key") or item.get("word")
                expand = item.get("expand") or item.get("to") or item.get("value") or item.get("means")
                if isinstance(alias, str) and isinstance(expand, str) and alias.strip() and expand.strip():
                    out[alias.strip().lower()] = " ".join(expand.split())
    return out


def _queries(value: Any) -> list[dict]:
    out: list[dict] = []
    items = value if isinstance(value, list) else [value]
    for item in items:
        if isinstance(item, str) and item.strip():
            out.append({"query": " ".join(item.split()), "label": ""})
        elif isinstance(item, dict):
            query = item.get("query") or item.get("text") or item.get("q") or item.get("search")
            if isinstance(query, str) and query.strip():
                label = item.get("label") or item.get("name") or item.get("title") or ""
                out.append({"query": " ".join(query.split()), "label": str(label)})
    return out


# --------------------------------------------------------------- SQLite


def _read_sqlite(path: Path) -> Source:
    """Read a kensaku SQLite store without knowing its schema.

    Every table is inspected for columns whose names mean a path, a query or a
    pair. That is more forgiving than `SELECT * FROM pins` and it is the only
    thing that works when the schema is somebody else's private business.
    """
    source = Source(path, "sqlite")
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return source
    conn.row_factory = sqlite3.Row
    try:
        tables = [
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        for table in tables:
            try:
                columns = [row["name"] for row in conn.execute(f'PRAGMA table_info("{table}")')]
            except sqlite3.Error:
                continue
            if not columns:
                continue
            lowered = {name.lower(): name for name in columns}
            path_column = next((lowered[key] for key in lowered if "path" in key), None)
            query_column = next((lowered[key] for key in lowered if key in ("query", "q", "text")), None)
            alias_column = next((lowered[key] for key in lowered if key in ("alias", "from", "word")), None)
            expand_column = next((lowered[key] for key in lowered if key in ("expand", "to", "value", "means")), None)
            label_column = next((lowered[key] for key in lowered if key in ("label", "name", "title")), None)
            try:
                rows = conn.execute(f'SELECT * FROM "{table}" LIMIT 5000').fetchall()
            except sqlite3.Error:
                continue
            table_lower = table.lower()
            for row in rows:
                if path_column and _key_hits(table_lower, PIN_WORDS):
                    value = row[path_column]
                    if isinstance(value, str) and _looks_like_path(value):
                        source.pins.append(value)
                elif alias_column and expand_column and _key_hits(table_lower, ALIAS_WORDS):
                    alias, expand = row[alias_column], row[expand_column]
                    if isinstance(alias, str) and isinstance(expand, str) and alias.strip() and expand.strip():
                        source.aliases[alias.strip().lower()] = " ".join(expand.split())
                elif query_column and _key_hits(table_lower, SAVED_WORDS):
                    query = row[query_column]
                    if isinstance(query, str) and query.strip():
                        label = row[label_column] if label_column else ""
                        source.saved.append(
                            {"query": " ".join(query.split()), "label": str(label or "")}
                        )
    except sqlite3.Error:
        pass
    finally:
        conn.close()
    return source


# ---------------------------------------------------------------- apply


def apply(store: Store, sources: Iterable[Source], write: bool = True) -> dict:
    """Copy everything found into the index. Returns what happened, per source."""
    report = {"sources": [], "pins": 0, "saved": 0, "aliases": 0, "missing": []}
    pins: list[str] = []
    saved: list[dict] = []
    aliases: dict[str, str] = {}
    for source in sources:
        report["sources"].append(source.as_dict())
        pins.extend(source.pins)
        saved.extend(source.saved)
        aliases.update(source.aliases)

    resolved = store.resolve_paths(pins)
    report["pins"] = len(resolved)
    report["missing"] = [path for path in dict.fromkeys(pins) if path not in resolved]
    if write:
        for file_id in dict.fromkeys(resolved.values()):
            store.set_pin(int(file_id), True)
        for item in saved:
            store.save_search(item["query"], item.get("label") or item["query"])
            report["saved"] += 1
        for alias, expand in aliases.items():
            try:
                store.set_alias(alias, expand)
                report["aliases"] += 1
            except sqlite3.Error:
                continue
        store.commit()
    else:
        report["saved"] = len(saved)
        report["aliases"] = len(aliases)
    return report
