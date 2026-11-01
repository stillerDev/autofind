"""Desktop and Start Menu shortcuts, so the app is something you double-click.

Autostart covers "always there"; this covers "and I can find it". Both point at
`pythonw.exe run.py`, so no console window flashes on the way in.
"""

from __future__ import annotations

import sys
from pathlib import Path

SHORTCUT_NAME = "AutoFind.lnk"


def supported() -> bool:
    return sys.platform == "win32"


def _desktop_dirs() -> list[Path]:
    """The current user's desktop only.

    PUBLIC\\Desktop is shared by every account on the machine; an app installed
    for one user has no business writing there.
    """
    import os

    profile = os.environ.get("USERPROFILE")
    if not profile:
        return []
    path = Path(profile) / "Desktop"
    return [path] if path.is_dir() else []


def _start_menu() -> Path | None:
    path = (
        Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows"
        / "Start Menu" / "Programs"
    )
    return path if path.is_dir() else None


def targets() -> list[Path]:
    """Every place a shortcut would be written, one per directory."""
    out = [path / SHORTCUT_NAME for path in _desktop_dirs()]
    start = _start_menu()
    if start is not None:
        out.append(start / SHORTCUT_NAME)
    return out


def _icon() -> Path | None:
    root = Path(__file__).resolve().parent.parent
    for name in ("icon.ico", "icon.png"):
        candidate = root / "web" / name
        if candidate.exists():
            return candidate
    return None


def _wanted(where: str) -> list[Path]:
    if where == "all":
        return targets()
    if where == "desktop":
        return [path / SHORTCUT_NAME for path in _desktop_dirs()]
    start = _start_menu()
    return [start / SHORTCUT_NAME] if start is not None else []


def create(where: str = "all") -> list[Path]:
    """Create the shortcuts. `where` is 'desktop', 'startmenu' or 'all'."""
    if not supported():
        raise RuntimeError("shortcuts are Windows-only for now")

    from win32com.client import Dispatch

    from .autostart import launcher_target

    target, arguments = launcher_target(hidden=False)
    workdir = str(Path(__file__).resolve().parent.parent)
    icon = _icon()

    shell = Dispatch("WScript.Shell")
    written: list[Path] = []
    for path in _wanted(where):
        shortcut = shell.CreateShortCut(str(path))
        shortcut.TargetPath = target
        shortcut.Arguments = arguments
        shortcut.WorkingDirectory = workdir
        shortcut.Description = "AutoFind — локальный поиск по файлам и скриншотам"
        if icon is not None:
            shortcut.IconLocation = f"{icon},0"
        shortcut.Save()
        written.append(path)
    return written


def remove(where: str = "all") -> list[Path]:
    removed: list[Path] = []
    for path in _wanted(where):
        try:
            path.unlink()
            removed.append(path)
        except OSError:
            continue
    return removed
