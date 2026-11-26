"""Desktop and Start Menu shortcuts, so the app is something you double-click.

Autostart covers "always there"; this covers "and I can find it". Both point at
`pythonw.exe run.py`, so no console window flashes on the way in.
"""

from __future__ import annotations

import sys
from pathlib import Path

SHORTCUT_NAME = "AutoFind.lnk"

# FOLDERID_Programs — the per-user Start Menu programs folder.
_FOLDERID_PROGRAMS = "A4115719-D62E-491D-AA7C-E74B8BE3B067"


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


def _known_folder(folder_id: str) -> Path | None:
    """SHGetKnownFolderPath for a FOLDERID GUID, or None when unavailable.

    Known Folders are localized and redirectable (OneDrive, corporate profiles),
    so a hard-coded `%USERPROFILE%/AppData/.../Start Menu/Programs` can point at a
    directory the user's Start Menu no longer uses.
    """
    if sys.platform != "win32":
        return None
    import ctypes
    import uuid
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        # GUID's in-memory layout is the little-endian form of the UUID bytes.
        _fields_ = [("data", ctypes.c_byte * 16)]

    path_ref = ctypes.POINTER(wintypes.WCHAR)()
    try:
        guid = GUID.from_buffer_copy(uuid.UUID(folder_id).bytes_le)
        shell32 = ctypes.windll.shell32
        shell32.SHGetKnownFolderPath.argtypes = [
            ctypes.POINTER(GUID),
            wintypes.DWORD,
            wintypes.HANDLE,
            ctypes.POINTER(ctypes.POINTER(wintypes.WCHAR)),
        ]
        shell32.SHGetKnownFolderPath.restype = ctypes.c_long
        result = shell32.SHGetKnownFolderPath(
            ctypes.byref(guid), 0, None, ctypes.byref(path_ref)
        )
        if result != 0 or not path_ref:
            return None
        value = path_ref.value
        return Path(value) if value else None
    except Exception:
        return None
    finally:
        try:
            ctypes.windll.ole32.CoTaskMemFree(path_ref)
        except Exception:
            pass


def _start_menu() -> Path | None:
    path = _known_folder(_FOLDERID_PROGRAMS)
    if path is None:
        # Older Windows, or a non-Windows test host: the classic per-user
        # location is still better than writing nothing.
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
    """Remove the shortcuts. `where` is 'desktop', 'startmenu' or 'all'."""
    if not supported():
        raise RuntimeError("shortcuts are Windows-only for now")
    removed: list[Path] = []
    for path in _wanted(where):
        try:
            path.unlink()
            removed.append(path)
        except OSError:
            continue
    return removed
