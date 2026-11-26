"""User-idle detection: crawl only when the user is not typing.

Indexing while a user is actively working steals disk and CPU from exactly
the session they are in — worst mid-typing, where the watcher's settle delay
fires while a document is being saved. `GetLastInputInfo` answers "how long
since the last keyboard/mouse event" in one cheap call, so the watcher can
defer whole cycles until the machine has been quiet for a while.

Idle here means *user* idle, not CPU idle: a compile or a video keeps the
user away from the keyboard and indexing is allowed, which is the right
trade for a search tool that runs on a workstation.
"""

from __future__ import annotations

import sys


def seconds_idle() -> float | None:
    """Seconds since the last user input, or None where it cannot be known."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(LASTINPUTINFO)
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        if not user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetTickCount.restype = ctypes.c_uint
        now = kernel32.GetTickCount()
        return _idle_from(info.dwTime, now)
    except Exception:
        return None


def _idle_from(last: int, now: int) -> float:
    """(now_ms - last_ms) as an unsigned 32-bit delta, in seconds."""
    delta = (now - last) & 0xFFFFFFFF
    return delta / 1000.0


class IdleGate:
    """True when the user has been idle long enough (or idle cannot be known).

    `seconds_idle_fn` is injectable so tests drive the gate with a fake
    clock. When idle detection is unavailable (a platform without it, or a
    call that failed) the gate stays open: deferring forever because the
    question cannot be answered would stop indexing entirely.
    """

    def __init__(self, min_idle: float = 30.0, seconds_idle_fn=seconds_idle) -> None:
        self.min_idle = min_idle
        self._seconds_idle_fn = seconds_idle_fn

    def ready(self) -> bool:
        seconds = self._seconds_idle_fn()
        if seconds is None:
            return True
        return seconds >= self.min_idle
