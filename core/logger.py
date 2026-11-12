"""Rotating server event log.

The server only printed to stdout so far, which is lost the moment the window
closes (or when the process runs detached from the autostart entry). This log
keeps the events that matter — index runs, watcher errors, boot — in
`~/.autofind/autofind.log` with a simple size-based rotation: when the file
grows past `max_mb`, it is shifted to `.1` and the oldest backup falls off.

Plain timestamped lines, not JSON: the log is read by humans diagnosing a
problem, and every consumer of structured data should use the API anyway.
Append-mode writes with one lock; events are rare (seconds apart at most), so
opening the file per write costs nothing and survives log cleanup by the user.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

MAX_MB = 2.0
BACKUPS = 2


class Logger:
    def __init__(self, path: str | Path, max_mb: float = MAX_MB, backups: int = BACKUPS) -> None:
        self.path = Path(path)
        self.max_bytes = max_mb * (1 << 20)
        self.backups = backups
        self._lock = threading.Lock()

    def write(self, message: str, level: str = "info") -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"{stamp} [{level}] {message}\n"
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._rotate_if_needed()
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(line)
            except OSError:
                # a broken log must never break the feature it observes
                pass

    def _rotate_if_needed(self) -> None:
        if not self.path.is_file() or self.path.stat().st_size < self.max_bytes:
            return
        # shift .2 -> delete, .1 -> .2, .log -> .1 (oldest falls off first)
        try:
            self.path.with_suffix(self.path.suffix + f".{self.backups}").unlink(
                missing_ok=True
            )
            for index in range(self.backups - 1, 0, -1):
                source = self.path.with_suffix(self.path.suffix + f".{index}")
                if source.is_file():
                    source.replace(
                        self.path.with_suffix(self.path.suffix + f".{index + 1}")
                    )
            self.path.replace(self.path.with_suffix(self.path.suffix + ".1"))
        except OSError:
            # rotation failed (file open elsewhere): just keep appending
            pass

    def tail(self, lines: int = 200) -> list[str]:
        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as handle:
                return handle.readlines()[-lines:]
        except OSError:
            return []
