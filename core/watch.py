"""Live index updates.

Windows: the directories push, we do not ask. A thread per root blocks in
ReadDirectoryChangesW and every create/write/delete lands here within
milliseconds; a batch is applied once the file has been quiet for `settle`
seconds, so a document being written is indexed once at the end instead of five
times while it grows. A slow full-tree resync (every minute or so) remains as a
safety net, because event streams are lossy by nature — the kernel reports a
buffer overflow instead of the missed events, and network shares are famously
unreliable at this.

Everywhere else (and for roots the OS refuses to watch): polling, one stat per
file every few seconds — cheap, and it works where the event APIs do not.

Writes are serialised with the full-index job: if a manual index is running the
watcher skips the cycle rather than fighting for the SQLite write lock.
"""

from __future__ import annotations

import struct
import sys
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
    mode: str = "poll"  # "events" (ReadDirectoryChangesW) or "poll"
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
            "mode": self.mode,
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
        self._thread = threading.Thread(target=self.run, name="autofind-watch", daemon=True)
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
            # Arm the stream first: changes landing while the snapshot walks
            # the tree land in the fan and get applied (idempotently) after.
            fan = make_event_fan(self.roots, self._stop)
            self.state.mode = "events" if fan is not None else "poll"
            previous = self._snapshot()
            self.state.removed += self._drop_missing(store, previous)
            self.state.last_run = time.time()
            if fan is not None:
                self._run_events(store, embedder, visual, fan)
            else:
                self._run_polling(store, embedder, visual, previous)
        except Exception as exc:
            self.state.last_error = f"{type(exc).__name__}: {exc}"
        finally:
            if store is not None:
                store.close()
            self.state.running = False

    def _run_polling(self, store, embedder, visual, previous: Snapshot) -> None:
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

    def _run_events(self, store, embedder, visual, fan: "EventFan") -> None:
        pending: dict[str, float] = {}  # path -> when the last event for it landed
        previous = self._snapshot()
        resync = max(self.interval * 15, 60.0)
        last_resync = time.time()
        while not self._stop.wait(0.2):
            if self.pause_when and self.pause_when():
                continue
            try:
                now = time.time()
                for path in fan.drain():
                    if self._owns(path):
                        pending[path] = now
                ready = [path for path, seen in pending.items() if now - seen >= self.settle]
                for path in ready:
                    del pending[path]
                if ready:
                    self._apply_events(store, embedder, visual, ready)
                # Events are lossy: an overflow is reported instead of the
                # missed names, and some editors write in ways the filters
                # miss. A slow full diff catches whatever the stream dropped.
                if now - last_resync >= resync:
                    last_resync = now
                    current = self._snapshot()
                    changed, removed = self._diff(previous, current)
                    if changed or removed:
                        self._apply(store, embedder, visual, changed, removed)
                    previous = current
                self.state.cycles += 1
            except Exception as exc:  # a broken batch must not kill the watcher
                self.state.last_error = f"{type(exc).__name__}: {exc}"
            if fan.broken:
                self.state.mode = "poll"
                self._run_polling(store, embedder, visual, self._snapshot())
                return

    def _apply_events(self, store: Store, embedder, visual, paths: list[str]) -> None:
        changed: list[str] = []
        removed: list[str] = []
        for path in paths:
            try:
                exists = Path(path).is_file()
            except OSError:
                exists = False
            (changed if exists else removed).append(path)
        # run_index re-filters through the scanner: kinds, size limits, privacy
        # and .gitignore parity are decided there, not here.
        if changed or removed:
            self._apply(store, embedder, visual, changed, removed)

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
            categories=self.config.privacy_categories,
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


# ------------------------------------------------------------- event fan (Win32)

# Windows pushes directory changes; reading the stream costs one blocked call
# per burst instead of one walk per interval. The ctypes plumbing is small and
# honest, and keeps the watcher dependency-free — the polling path below stays
# for every platform (and every root) where this stream is not available.

RDCW_FILTER = (  # FILE_NOTIFY_CHANGE_*
    0x1  # FILE_NAME: create/delete/rename
    | 0x2  # DIR_NAME: new folders arrive with their contents
    | 0x8  # SIZE
    | 0x10  # LAST_WRITE
    | 0x40  # CREATION
)

BUFFER_BYTES = 64 * 1024
ACTIONS = {1: "added", 2: "removed", 3: "modified", 4: "renamed-old", 5: "renamed-new"}


class EventFan:
    """Changed paths from every watched root, drained from one place.

    `broken` flips on when a root handle dies (a watched folder deleted, a
    network drive gone); the watcher then falls back to polling rather than
    pretending to see changes.
    """

    def __init__(self, roots: Sequence[str], stop: threading.Event) -> None:
        self._lock = threading.Lock()
        self._events: set[str] = set()
        self._overflow = threading.Event()
        self.broken = False
        self._threads: list[threading.Thread] = []
        for root in roots:
            thread = threading.Thread(
                target=self._watch_root, args=(root, stop), name=f"autofind-fswatch", daemon=True
            )
            self._threads.append(thread)
            thread.start()

    def drain(self) -> list[str]:
        with self._lock:
            paths = list(self._events)
            self._events.clear()
        if self._overflow.is_set():
            self._overflow.clear()
            # Signal the caller (via a reserved sentinel) that a full resync is
            # wanted; the watcher's slow resync covers this anyway, so an
            # overflow just nudges it by returning an empty batch.
        return paths

    def _push(self, root: str, name: str) -> None:
        with self._lock:
            self._events.add(f"{root}\\{name}")

    def _watch_root(self, root: str, stop: threading.Event) -> None:
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateFileW.restype = wintypes.HANDLE
            kernel32.CreateFileW.argtypes = [
                wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
            ]
            kernel32.ReadDirectoryChangesW.restype = wintypes.BOOL
            kernel32.ReadDirectoryChangesW.argtypes = [
                wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, wintypes.BOOL,
                wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
                wintypes.LPVOID,
            ]
            invalid = wintypes.HANDLE(-1).value
            handle = kernel32.CreateFileW(
                root,
                0x1,  # FILE_LIST_DIRECTORY
                0x7,  # FILE_SHARE_READ | WRITE | DELETE
                None,
                3,  # OPEN_EXISTING
                0x02000000,  # FILE_FLAG_BACKUP_SEMANTICS: needed for directories
                None,
            )
            if handle in (None, invalid):
                self.broken = True  # root is a share or gone: poll instead
                return
            try:
                buffer = ctypes.create_string_buffer(BUFFER_BYTES)
                returned = wintypes.DWORD()
                while not stop.is_set():
                    # The synchronous call blocks until events arrive or the
                    # root disappears; stop is enforced by process exit (the
                    # thread is daemon) and by the broken-root path below.
                    ok = kernel32.ReadDirectoryChangesW(
                        handle, buffer, BUFFER_BYTES, True, RDCW_FILTER,
                        ctypes.byref(returned), None, None,
                    )
                    if not ok or returned.value == 0:
                        error = ctypes.get_last_error()
                        if error == 1022:  # ERROR_NOTIFY_ENUM_DIR: buffer overflow
                            self._overflow.set()
                            continue
                        self.broken = True  # deleted root or dead handle
                        return
                    for _action, name in _parse_events(buffer.raw[: returned.value]):
                        self._push(root, name)
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            self.broken = True


def _parse_events(raw: bytes) -> list[tuple[int, str]]:
    """One FILE_NOTIFY_INFORMATION chain from the raw buffer."""
    events: list[tuple[int, str]] = []
    offset = 0
    while True:
        next_offset, action, name_bytes = struct.unpack_from("<III", raw, offset)
        name = raw[offset + 12 : offset + 12 + name_bytes].decode("utf-16-le").rstrip("\x00")
        if name:
            events.append((action, name))
        if next_offset == 0:
            break
        offset += next_offset
    return events


def make_event_fan(roots: Sequence[str], stop: threading.Event) -> EventFan | None:
    """The push-based fan on Windows, None where it cannot work."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes  # noqa: F401

        return EventFan(roots, stop)
    except Exception:
        return None


def watch_forever(watcher: Watcher) -> int:
    """Blocking run for the CLI: prints one line per batch until Ctrl+C."""
    watcher.start()
    mode = "events (ReadDirectoryChangesW)" if watcher.state.mode == "events" else "polling"
    print(f"watching {len(watcher.roots)} root(s) via {mode}")
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
