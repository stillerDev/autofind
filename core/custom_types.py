"""User-defined file-type tabs.

A custom type is a name plus a set of filename masks (`*.jpg`). It lives in
`~/.autofind/types.json` next to the index, never inside a scanned tree: the
catalog is the user's own data, and rebuilding the index must not forget it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

TYPES_FILE = "types.json"
MAX_TYPES = 50
MAX_MASKS = 32
MAX_NAME = 40

_ID = re.compile(r"^[a-z0-9_-]{1,32}$")
_SLUG = re.compile(r"[^a-z0-9_-]+")


@dataclass(slots=True)
class CustomType:
    id: str
    name: str
    masks: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "masks": list(self.masks)}


def slugify(name: str) -> str:
    slug = _SLUG.sub("-", name.strip().lower()).strip("-_")
    return slug[:32] or "type"


def normalise_mask(raw: str) -> str | None:
    """Turn `jpg`, `.JPG` or `C:\\pics\\*.png` into a lowercase basename glob."""
    text = str(raw or "").strip().replace("\\", "/")
    if not text:
        return None
    text = text.rsplit("/", 1)[-1].strip().lower()
    if not text or not any(char.isalnum() for char in text):
        return None
    if not any(char in text for char in "*?["):
        if "." in text:
            text = "*" + text[text.index("."):]
        else:
            text = "*." + text
    if text in {"*", "*.*", "*?", "?*", "."}:
        return None
    return text


def _unique(type_id: str, seen: set[str]) -> str:
    if type_id not in seen:
        return type_id
    base = type_id
    number = 2
    while f"{base}-{number}" in seen:
        number += 1
    return f"{base}-{number}"


def coerce_types(rows) -> list[CustomType]:
    """Sanitise a request body or a file into the shape the rest of the app uses."""
    if not isinstance(rows, list):
        raise ValueError("types must be a list")
    out: list[CustomType] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()[:MAX_NAME]
        if not name:
            continue
        raw_id = str(row.get("id") or "").strip().lower()
        type_id = _unique(raw_id if _ID.match(raw_id) else slugify(name), seen)
        seen.add(type_id)
        masks: list[str] = []
        raw_masks = row.get("masks")
        for mask in raw_masks if isinstance(raw_masks, list) else []:
            normalised = normalise_mask(mask)
            if normalised and normalised not in masks:
                masks.append(normalised)
        out.append(CustomType(type_id, name, masks[:MAX_MASKS]))
        if len(out) >= MAX_TYPES:
            break
    return out


def read_types(home: Path) -> list[CustomType]:
    try:
        data = json.loads((home / TYPES_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = data.get("types") if isinstance(data, dict) else None
    return coerce_types(rows or [])


def save_types(home: Path, types: list[CustomType]) -> None:
    try:
        home.mkdir(parents=True, exist_ok=True)
        (home / TYPES_FILE).write_text(
            json.dumps(
                {"version": 1, "types": [item.as_dict() for item in types]},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def find_type(types: list[CustomType], type_id: str) -> CustomType | None:
    for item in types:
        if item.id == type_id:
            return item
    return None
