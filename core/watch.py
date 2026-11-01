"""Live index updates.

There is no watchdog dependency on purpose: one stat per file every few seconds
is cheap, and polling works on network shares and cloud-synced folders where
inotify and ReadDirectoryChanges are famously unreliable. A file is only picked
up after it has been quiet for `settle` seconds, so a document being written is
indexed once at the end instead of five times while it grows.

Writes are serialised with the full-index job: if a manual index is running the
watcher skips the cycle rather than fighting for the SQLite write lock.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from .config import IndexConfig
from .embed import get_embedder, get_visual_embedder
from .indexer import run_index
from .scanner import iter_entries
from .store import Store

Snapshot = dict[str, tuple[int, float]]


@dataclass(slots=True)
class WatchState:
    running: bool = False
    cycles: int = 0
    added: int = 0
    updated: int = 0
    removed: int = 0
    indexed_chunks: int = 0
    last_run: float = 0.0
    last_batch: int = 0
    last_error: str | None = None
    roots: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "running": self.running,
            "cycles": self.cycles,
            "added": self.added,
            "updated": self.updated,
            "removed": self.removed,
            "indexed_chunks": self.indexed_chunks,
            "last_run": self.last_run,
            "last_batch": self.last_batch,
            "last_error": self.last_error,
            "roots": list(self.roots),
        }


class Watcher:
    def __init__(
        self,
        roots: Sequence[str | Path],
        config: IndexConfig,
        db_path: str | Path,
        interval: float | None = None,
        settle: float | None = None,
        on_batch: Callable[[list[str], list[str], dict], None] | None = None,
        pause_when: Callable[[], bool] | None = None,
    ) -> None:
        self.roots = [str(Path(root).expanduser()) for root in roots]
        self.config = config
        self.db_path = Path(db_path)
        self.interval = interval if interval is not None else config.watch_interval
        self.settle = settle if settle is not None else config.watch_settle
        self.on_batch = on_batch
        self.pause_when = pause_when
        self.state = WatchState(roots=self.roots)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._skip_prefix = str(config.home.resolve()).lower()
        self._skip_path = str(self.db_path.resolve()).lower()

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self.state.running = True
        self._thread = threading.Thread(target=self.run, name="semfind-watch", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout)
        self.state.running = False

    def run(self) -> None:
        store = None
        try:
            embedder = get_embedder("auto", self.config.model_dir)
            visual = get_visual_embedder(self.config.model_dir)
            store = Store(self.db_path, embedder.name, embedder.dim)
            previous = self._snapshot()
            self.state.removed += self._drop_missing(store, previous)
            self.state.last_run = time.time()
            while not self._stop.wait(self.interval):
                if self.pause_when and self.pause_when():
                    continue
                try:
                    current = self._snapshot()
                    changed, removed = self._diff(previous, current)
                    if changed or removed:
                        self._apply(store, embedder, visual, changed, removed)
                    previous = current
                    self.state.cycles += 1
                except Exception as exc:  # a broken batch must not kill the watcher
                    self.state.last_error = f"{type(exc).__name__}: {exc}"
        except Exception as exc:
            self.state.last_error = f"{type(exc).__name__}: {exc}"
        finally:
            if store is not None:
                store.close()
            self.state.running = False

    # ---------------------------------------------------------------- diffing

    def _snapshot(self) -> Snapshot:
        now = time.time()
        state: Snapshot = {}
        for entry in iter_entries(
            self.roots,
            excluded_dirs=self.config.excluded_dirs,
            max_bytes=self.config.max_file_bytes,
            min_bytes=self.config.min_file_bytes,
            privacy=self.config.privacy and not self.config.allow_sensitive,
        ):
            if not self._owns(entry.path):
                continue
            if now - entry.mtime < self.settle:
                continue
            state[entry.path] = (entry.size, entry.mtime)
        return state

    def _owns(self, path: str) -> bool:
        lowered = path.lower()
        if lowered == self._skip_path or lowered.startswith(self._skip_prefix):
            return False
        return True

    def _diff(self, previous: Snapshot, current: Snapshot) -> tuple[list[str], list[str]]:
        changed = [path for path, stamp in current.items() if previous.get(path) != stamp]
        removed = [path for path in previous if path not in current]
        return changed, removed

    def _drop_missing(self, store: Store, known: Snapshot) -> int:
        """Forget files the index still lists under our roots but disk no longer has.

        Without this pass a watcher started after a deletion would keep serving
        a file that is gone: it only knows about changes it saw itself.
        """
        removed = 0
        roots = [root.lower().rstrip("\\/") for root in self.roots]
        for row in store.conn.execute("SELECT id, path FROM files").fetchall():
            path = row["path"]
            lowered = path.lower()
            if not any(lowered == root or lowered.startswith(root + "\\") or lowered.startswith(root + "/") for root in roots):
                continue
            if path in known or Path(path).exists():
                continue
            store.drop_file(int(row["id"]))
            removed += 1
        if removed:
            store.commit()
        return removed

    # ---------------------------------------------------------------- applying

    def _apply(self, store: Store, embedder, visual, changed: list[str], removed: list[str]) -> None:
        report: dict = {"indexed": 0, "chunks": 0, "removed": 0, "failed": 0}
        if changed:
            stats = run_index(changed, self.config, store, embedder, visual)
            report["indexed"] = stats.indexed
            report["chunks"] = stats.chunks
            report["failed"] = stats.failed
            self.state.indexed_chunks += stats.chunks
        for path in removed:
            row = store.get_file(path)
            if row is not None:
                store.drop_file(int(row["id"]))
                report["removed"] += 1
        store.commit()

        self.state.added += report["indexed"]
        self.state.removed += report["removed"]
        self.state.last_batch = len(changed) + len(removed)
        self.state.last_run = time.time()
        if self.on_batch:
            self.on_batch(changed, removed, report)


def watch_forever(watcher: Watcher) -> int:
    """Blocking run for the CLI: prints one line per batch until Ctrl+C."""
    watcher.start()
    print(f"watching {len(watcher.roots)} root(s), interval {watcher.interval}s")
    for root in watcher.roots:
        print(f"  {root}")
    print("ctrl+c to stop")
    try:
        while watcher.state.running:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        watcher.stop()
    state = watcher.state
    print(
        f"\nstopped after {state.cycles} cycles: "
        f"{state.added} indexed, {state.removed} removed, {state.indexed_chunks} chunks"
    )
    if state.last_error:
        print(f"last error: {state.last_error}")
    return 0
