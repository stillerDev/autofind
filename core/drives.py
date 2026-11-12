"""Drive-type detection: keep network shares out of the crawl by default.

Reading a network share is slow but survivable; watching it is where things
fall apart — ReadDirectoryChangesW over SMB drops events under load, a crawl
holds the share's handle for minutes, and a flaky VPN turn makes the watcher
report half the tree as deleted (the root handle died, `broken` flips, and
the safety-net resync then has to trust a partial snapshot). So network
roots are refused until `--allow-remote` / `"allow_remote": true` says the
user has thought about it.

`GetDriveTypeW` answers in one call; the injectable `type_fn` keeps the
guard testable without a real share.
"""

from __future__ import annotations

import sys
from pathlib import Path

DRIVE_TYPES = {
    0: "unknown",   # DRIVE_UNKNOWN
    1: "none",      # DRIVE_NO_ROOT_DIR
    2: "removable",  # DRIVE_REMOVABLE
    3: "fixed",     # DRIVE_FIXED
    4: "remote",    # DRIVE_REMOTE: a network share
    5: "cdrom",     # DRIVE_CDROM
    6: "ramdisk",   # DRIVE_RAMDISK
}


def drive_type(path: str | Path) -> str:
    """The Windows drive type of the volume holding `path` ('fixed' elsewhere)."""
    if sys.platform != "win32":
        return "fixed"
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        root = _drive_root(path)
        if root is None:
            return "fixed"
        code = int(kernel32.GetDriveTypeW(ctypes.c_wchar_p(root)))
        return DRIVE_TYPES.get(code, "unknown")
    except Exception:
        return "fixed"


def _drive_root(path: str | Path) -> str | None:
    """'C:\\\\' from a full path; None when the path has no drive letter."""
    drive, _ = Path(str(path)).drive, None
    if not drive:
        return None
    return drive + "\\" if not drive.endswith("\\") else drive


def remote_roots(roots, type_fn=drive_type) -> list[str]:
    """The subset of `roots` that live on network volumes."""
    return [str(root) for root in roots if type_fn(root) == "remote"]
