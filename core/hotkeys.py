"""User-configurable keyboard shortcuts.

The defaults are the shortcuts the app has always had; a user can replace them
without touching the source. The file lives next to the index in `~/.autofind`
so a backup carries it, and it is plain JSON because a shortcut is a preference,
not a secret.

A binding is a chord such as ``ctrl+shift+d``. Parsing is deliberately strict:
one non-modifier key plus any modifiers, written in a canonical order. That is
what lets the settings screen compare two bindings without guessing whether
``Shift+Ctrl+D`` and ``ctrl+shift+d`` are the same chord — they are, and a
person who types one while the other is taken must see the conflict.
"""

from __future__ import annotations

import json
from pathlib import Path

HOTKEYS_FILE = "hotkeys.json"

MODIFIER_ORDER = ("ctrl", "alt", "shift", "meta")
_MODIFIER_ALIASES = {
    "ctrl": "ctrl", "control": "ctrl",
    "alt": "alt", "option": "alt",
    "shift": "shift",
    "meta": "meta", "win": "meta", "cmd": "meta", "command": "meta",
}
_KEY_ALIASES = {
    " ": "space", "spacebar": "space",
    "esc": "escape",
    "del": "delete", "ins": "insert",
    "return": "enter",
    "+": "plus",
    "-": "minus",
    "arrowup": "up", "arrowdown": "down", "arrowleft": "left", "arrowright": "right",
}

DEFAULTS: dict[str, list[str]] = {
    "focus": ["/"],
    "search": ["ctrl+k"],
    "settings": ["ctrl+,"],
    "panel": ["ctrl+shift+i"],
    "duplicates": ["ctrl+shift+d"],
    "preview": ["space"],
    "open": ["enter"],
    "reveal": ["shift+enter"],
    "next": ["down"],
    "prev": ["up"],
    "select": ["x"],
    "export": ["ctrl+shift+e"],
}

# The order the settings screen shows them in; also the list of valid ids.
ACTIONS = tuple(DEFAULTS)


def normalize_chord(value: str) -> str | None:
    """``"Shift + Ctrl + D"`` -> ``"ctrl+shift+d"``; ``None`` when invalid.

    A chord must contain exactly one non-modifier key. A lone modifier is not a
    shortcut, and treating it as one would make ``ctrl+`` swallow every ``ctrl``
    press in the window.
    """
    text = str(value or "").strip()
    if not text:
        return None
    # A literal plus is the key itself; a trailing separator is not a valid key.
    if text == "+":
        return "plus"
    parts = [part.strip() for part in text.split("+")]
    modifiers: set[str] = set()
    key = ""
    for part in parts:
        if not part:
            continue
        lowered = part.lower()
        modifier = _MODIFIER_ALIASES.get(lowered)
        if modifier is not None:
            modifiers.add(modifier)
            continue
        if key:
            return None  # two non-modifier keys is not a chord the UI can show
        key = _KEY_ALIASES.get(lowered, lowered)
    if not key:
        return None
    ordered = [name for name in MODIFIER_ORDER if name in modifiers]
    return "+".join([*ordered, key])


def normalize_combo_list(values) -> list[str]:
    """Normalise a list of chords, dropping empties and exact repeats."""
    if not isinstance(values, (list, tuple)):
        return []
    out: list[str] = []
    for item in values:
        chord = normalize_chord(item)
        if chord and chord not in out:
            out.append(chord)
    return out


def coerce_hotkeys(payload) -> dict[str, list[str]]:
    """Accept a partial or full mapping and return every known action.

    An action omitted from the payload keeps its default, which is what a save
    from an older settings screen means. An action sent as an empty list is
    deliberately disabled and is kept that way.
    """
    source = payload if isinstance(payload, dict) else {}
    out: dict[str, list[str]] = {}
    for action in ACTIONS:
        if action in source:
            out[action] = normalize_combo_list(source[action])
        else:
            out[action] = list(DEFAULTS[action])
    return out


def read_hotkeys(home: Path) -> dict[str, list[str]]:
    try:
        data = json.loads((home / HOTKEYS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {action: list(combos) for action, combos in DEFAULTS.items()}
    return coerce_hotkeys(data)


def save_hotkeys(home: Path, hotkeys: dict[str, list[str]]) -> dict[str, list[str]]:
    clean = coerce_hotkeys(hotkeys)
    try:
        home.mkdir(parents=True, exist_ok=True)
        target = home / HOTKEYS_FILE
        temporary = target.with_suffix(target.suffix + ".part")
        temporary.write_text(
            json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(target)
    except OSError:
        pass
    return clean


def _parts(chord: str) -> tuple[frozenset[str], str]:
    pieces = chord.split("+")
    return frozenset(pieces[:-1]), pieces[-1]


def conflicts(hotkeys: dict[str, list[str]]) -> list[dict]:
    """Every pair of bindings that clash, for the settings screen.

    ``duplicate`` is the same chord on two actions — the second action can never
    fire. ``similar`` is the same key with a modifier subset (``ctrl+d`` against
    ``ctrl+shift+d``): not impossible, but it is the pair users press by mistake,
    so it is reported with the same red treatment and a softer word.
    """
    rows: list[dict] = []
    flat: list[tuple[str, str, frozenset[str], str]] = []
    for action in ACTIONS:
        for chord in hotkeys.get(action, []):
            modifiers, key = _parts(chord)
            flat.append((action, chord, modifiers, key))
    for index, first in enumerate(flat):
        for second in flat[index + 1:]:
            if first[0] == second[0]:
                continue
            if first[1] == second[1]:
                rows.append({
                    "a": {"action": first[0], "chord": first[1]},
                    "b": {"action": second[0], "chord": second[1]},
                    "type": "duplicate",
                })
                continue
            if first[3] != second[3]:
                continue
            small, large = sorted((first[2], second[2]), key=len)
            if small < large:
                rows.append({
                    "a": {"action": first[0], "chord": first[1]},
                    "b": {"action": second[0], "chord": second[1]},
                    "type": "similar",
                })
    return rows
