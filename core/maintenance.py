"""Data-directory maintenance: what is there, what can go, and what a reset
should keep.

Two problems this solves, the same two Kensaku's maintenance.rs names:

**Reset was all-or-nothing.** Wiping the data directory also deleted the
downloaded OCR and model packs (tens of MB), so "reset to fix a bad index"
cost a re-download before search could work again. A reset keeps those
unless the caller explicitly asks otherwise.

**Nothing ever cleaned up after itself.** A force-import leaves the old index
in ``import-backup/`` forever; a killed download leaves a ``.part`` file; an
updater whose process died between stage and install leaves an
``autofind-update-<pid>-<hex>`` temp directory that no later process owns.
Over a few releases that is hundreds of megabytes of dead weight in a user's
profile, which is exactly what makes an upgrade feel like it left a mess.

AutoFind has no ``index.legacy-*`` renames (schema migrations live inside the
database) and thumbnails are stored in the database, not a directory — so the
cleanup surface is smaller than Kensaku's, and the inventory reflects that.
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

# Directories that hold downloaded artifacts, not user state. A reset keeps
# these by default: they are reproducible only by downloading them again, and
# they are not what a reset is meant to forget.
PACK_DIRS = ("models", "ocr-indra")

# Backup directories created by a forced import (the old index is moved aside
# so the new one can land). They are safe to clean — the user already chose to
# replace the index — but the newest one may be the only copy of the index a
# user regretted replacing, so exactly one survives a cleanup.
_IMPORT_BACKUP = "import-backup"

# A staged download is never a finished write: every ``.part`` file in the
# home directory is abandoned work (api.background and hotkeys write temp
# files with this suffix and replace them on success).
_PART_SUFFIX = ".part"

# Updater staging directories live in the system temp folder, named like
# ``autofind-update-<pid>-<hex>``. A directory from a process that is still
# running belongs to that process; anything this old from a dead process is
# abandoned staging.
_UPDATE_TEMP_PREFIX = "autofind-update-"
_KEEP_TEMP_MAX_AGE = 24 * 3600

# ".part" and temp directories younger than this are not removed: a download
# running right now would have its staging deleted out from under it.
_GRACE_SECONDS = 60 * 60


@dataclass
class Inventory:
    """What the data directory holds, in bytes and counts, for the UI and
    the ``autofind maintenance`` report."""

    home: Path
    total_bytes: int = 0
    db_bytes: int = 0
    pack_bytes: dict[str, int] = field(default_factory=dict)
    import_backups: int = 0
    import_backup_bytes: int = 0
    part_files: int = 0
    part_bytes: int = 0
    update_temp_bytes: int = 0
    log_bytes: int = 0
    problems: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [f"data directory: {self.home}", f"total: {_size(self.total_bytes)}"]
        lines.append(f"index: {_size(self.db_bytes)}")
        for name, size in self.pack_bytes.items():
            lines.append(f"pack {name}: {_size(size)}")
        if self.import_backups:
            lines.append(
                f"import backups: {self.import_backups} ({_size(self.import_backup_bytes)})"
            )
        if self.part_files:
            lines.append(f"stale .part files: {self.part_files} ({_size(self.part_bytes)})")
        if self.update_temp_bytes:
            lines.append(f"update temp dirs: {_size(self.update_temp_bytes)}")
        lines.append(f"logs: {_size(self.log_bytes)}")
        return "\n".join(lines)


def dir_size(path: Path) -> int:
    """Total bytes of every file under `path`; unreadable entries count as 0
    because maintenance must never fail on a permission it does not have."""
    total = 0
    try:
        entries = list(path.iterdir())
    except OSError:
        return 0
    for entry in entries:
        try:
            if entry.is_dir() and not entry.is_symlink():
                total += dir_size(entry)
            elif entry.is_file():
                total += entry.stat().st_size
        except OSError:
            continue
    return total


def _current_pids() -> set[int]:
    """This process and, on Windows, every running PID — the updater stage dir
    embeds its owner's PID, and guessing "dead" from a PID reuse would delete
    a live download. Cheap: one 1 KB API call is nothing next to a deleted
    installer."""
    pids = {os.getpid(), os.getppid()}
    # tasklist is the only stdlib-compatible way without psutil; skip failure
    # silently — the age grace period below is the real safety net.
    try:
        import subprocess

        out = subprocess.run(
            ["tasklist", "/fo", "csv", "/nh"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
        for line in out.splitlines():
            try:
                pids.add(int(line.split('","')[1]))
            except (IndexError, ValueError):
                continue
    except Exception:
        pass
    return pids


def _update_temp_dirs(home: Path) -> list[Path]:
    """Updater staging directories in the system temp folder. The path comes
    from updater._stage_dir; keeping the pattern in sync here is why the
    constant lives in one place above."""
    temp = Path(os.environ.get("TEMP") or tempfile_dir())
    if not temp.is_dir():
        return []
    return sorted(temp.glob(f"{_UPDATE_TEMP_PREFIX}*"))


def tempfile_dir() -> str:
    """The system temp directory, without importing tempfile's whole world."""
    import tempfile

    return tempfile.gettempdir()


def inventory(home: Path) -> Inventory:
    """Measure everything the report needs. Read-only."""
    result = Inventory(home=Path(home))
    result.total_bytes = dir_size(result.home)
    for suffix in ("", "-wal", "-shm"):
        try:
            result.db_bytes += (result.home / f"index.db{suffix}").stat().st_size
        except OSError:
            continue
    for pack in PACK_DIRS:
        result.pack_bytes[pack] = dir_size(result.home / pack)
    backup = result.home / _IMPORT_BACKUP
    if backup.is_dir():
        result.import_backups = 1
        result.import_backup_bytes = dir_size(backup)
    for path in _part_files(result.home):
        try:
            result.part_files += 1
            result.part_bytes += path.stat().st_size
        except OSError:
            continue
    for stage in _update_temp_dirs(result.home):
        result.update_temp_bytes += dir_size(stage)
    for log in sorted(result.home.glob("autofind.log*")):
        try:
            result.log_bytes += log.stat().st_size
        except OSError:
            continue
    return result


def _part_files(home: Path) -> list[Path]:
    try:
        return sorted(home.glob(f"*{_PART_SUFFIX}"))
    except OSError:
        return []


def cleanup_stale_artifacts(home: Path) -> list[str]:
    """Remove what no process can still be using. Returns what was done, for
    the log; every step is independent, so one failure does not stop the rest.

    * import backups: the newest stays (the only copy of a regretted import),
      older ones go.
    * ``.part`` files: abandoned staging, removed when older than the grace
      period.
    * updater temp dirs: those of dead processes, and only when older than a
      day — a live updater's stage dir must never be touched.
    """
    home = Path(home)
    done: list[str] = []

    backup = home / _IMPORT_BACKUP
    if backup.is_dir():
        # One dir today (force-import overwrites in place); if the layout ever
        # grows siblings, the newest by mtime survives.
        siblings = sorted(
            (p for p in home.glob(f"{_IMPORT_BACKUP}*") if p.is_dir()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for stale in siblings[1:]:
            _rmtree(stale)
            done.append(f"removed old backup {stale.name}")

    now = time.time()
    for part in _part_files(home):
        try:
            if now - part.stat().st_mtime < _GRACE_SECONDS:
                continue
            part.unlink()
            done.append(f"removed stale {part.name}")
        except OSError:
            continue

    live_pids: set[int] | None = None
    for stage in _update_temp_dirs(home):
        try:
            suffix = stage.name[len(_UPDATE_TEMP_PREFIX):]
            pid = int(suffix.split("-", 1)[0])
        except (IndexError, ValueError):
            pid = None
        if pid is not None and live_pids is None:
            live_pids = _current_pids()
        if pid is not None and pid in live_pids:
            continue  # some process may still be writing here
        try:
            if now - stage.stat().st_mtime < _KEEP_TEMP_MAX_AGE:
                continue
            _rmtree(stage)
            done.append(f"removed update temp {stage.name}")
        except OSError:
            continue
    return done


def _rmtree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


@dataclass
class ResetPlan:
    """What a reset would remove and what it would keep — shown before the
    ``--yes`` that makes it happen."""

    remove: list[str] = field(default_factory=list)
    keep: list[str] = field(default_factory=list)


def reset_plan(home: Path, remove_packs: bool = False) -> ResetPlan:
    """Dry-run view of `reset`. Everything in the home directory goes except
    the packs (unless asked), because a pack is not user state."""
    home = Path(home)
    plan = ResetPlan()
    try:
        entries = sorted(home.iterdir(), key=lambda p: p.name)
    except OSError:
        return plan
    for entry in entries:
        if entry.name in PACK_DIRS:
            if remove_packs:
                plan.remove.append(entry.name)
            else:
                plan.keep.append(entry.name)
        else:
            plan.remove.append(entry.name)
    return plan


def apply_reset(home: Path, remove_packs: bool = False) -> tuple[int, int]:
    """Delete everything in the data directory except the packs (by default).

    Returns (removed, failed) counts. The packs are skipped, not deleted and
    recreated — deleting a live pack out from under a running OCR session is
    exactly the kind of half-reset that makes users re-download.
    """
    home = Path(home)
    removed = failed = 0
    try:
        entries = list(home.iterdir())
    except OSError:
        return (0, 0)
    for entry in entries:
        if entry.name in PACK_DIRS and not remove_packs:
            continue
        try:
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
            removed += 1
        except OSError:
            failed += 1
    return (removed, failed)


def _size(value: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{value} {unit}"
        value /= 1024
    return f"{value} B"
