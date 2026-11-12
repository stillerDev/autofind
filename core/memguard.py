"""RAM soft limit: watch private memory, drop caches when it is too high.

A desktop search tool is a guest on the machine. The heavy holders are all
rebuildable caches — the vector matrix in `VectorCache`, the name index in
`NAME_CACHE`, the duplicate-report cache and the pooled read Stores — so when
private memory crosses the limit, dropping them costs seconds on the next
query and frees hundreds of MB immediately.

Soft means soft: the limit is advisory, checked between requests, and the
eviction only touches rebuildable state — never an index being written or a
job in flight. The next query rebuilds what it needs, which is exactly the
degradation a "soft" limit promises.
"""

from __future__ import annotations

import os
import sys
import threading
import time


def process_rss_mb() -> float | None:
    """This process's private (working set) memory in MB, None if unknown."""
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            counters = PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
            handle = ctypes.WinDLL("kernel32", use_last_error=True).GetCurrentProcess()
            if not psapi.GetProcessMemoryInfo(
                wintypes.HANDLE(handle), ctypes.byref(counters), counters.cb
            ):
                return None
            return counters.WorkingSetSize / (1 << 20)
        except Exception:
            return None
    try:
        # Linux/macOS fallback: the VmRSS line of /proc, in kB.
        with open("/proc/self/status", "r") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return float(line.split()[1]) / 1024.0
    except OSError:
        pass
    return None


class SoftLimit:
    """A memory ceiling with a cooldown, so eviction cannot thrash.

    The cooldown matters: RSS does not drop the instant a cache is cleared —
    the allocator holds freed pages — so without one, every check right after
    an eviction would clear the *next* freshly built cache too, and the app
    would spend its life rebuilding instead of searching.
    """

    def __init__(self, limit_mb: float | None = None, rss_fn=process_rss_mb,
                 cooldown_s: float = 60.0) -> None:
        if limit_mb is None:
            limit_mb = float(os.environ.get("AUTOFIND_RAM_LIMIT_MB") or 0)
        self.limit_mb = float(limit_mb) if limit_mb > 0 else None
        self._rss_fn = rss_fn
        self._cooldown = cooldown_s
        self._last_evict = 0.0
        self._lock = threading.Lock()
        self.last_reason: str | None = None

    def exceeded(self) -> bool:
        """True when the limit is set, crossed, and the cooldown has passed."""
        if self.limit_mb is None:
            return False
        with self._lock:
            if time.time() - self._last_evict < self._cooldown:
                return False
        rss = self._rss_fn()
        if rss is None:
            return False
        return rss > self.limit_mb

    def mark_evicted(self, reason: str) -> None:
        with self._lock:
            self._last_evict = time.time()
        self.last_reason = reason
