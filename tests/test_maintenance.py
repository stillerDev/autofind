"""Maintenance: inventory, stale-artifact cleanup and reset-with-packs.

Ports of Kensaku maintenance.rs's guarantees: a reset keeps the packs, the
newest import backup survives, a live process's staging is never removed, and
the inventory is read-only.
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from core.maintenance import (
    PACK_DIRS,
    apply_reset,
    cleanup_stale_artifacts,
    inventory,
    reset_plan,
)


def _old(path: Path, seconds: float = 60 * 60 * 2) -> None:
    """Make a file look older than every grace period."""
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


class InventoryTests(unittest.TestCase):
    def test_inventory_counts_packs_db_and_junk(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "index.db").write_bytes(b"x" * 1000)
            (home / "index.db-wal").write_bytes(b"w" * 100)
            (home / "ocr-indra").mkdir()
            (home / "ocr-indra" / "det.onnx").write_bytes(b"m" * 500)
            (home / "import-backup").mkdir()
            (home / "import-backup" / "index.db").write_bytes(b"b" * 200)
            (home / "screenshot.png.part").write_bytes(b"p" * 50)
            (home / "autofind.log").write_text("log line")
            report = inventory(home)
        self.assertEqual(report.total_bytes, 1000 + 100 + 500 + 200 + 50 + len("log line"))
        self.assertEqual(report.db_bytes, 1100)
        self.assertEqual(report.pack_bytes["ocr-indra"], 500)
        self.assertEqual(report.import_backups, 1)
        self.assertEqual(report.part_files, 1)
        self.assertEqual(report.part_bytes, 50)
        self.assertIn("index:", report.summary())
        self.assertIn("pack ocr-indra:", report.summary())

    def test_inventory_of_a_missing_directory_is_all_zero(self):
        report = inventory(Path(tempfile.gettempdir()) / "autofind-does-not-exist-xyz")
        self.assertEqual(report.total_bytes, 0)
        self.assertEqual(report.db_bytes, 0)


class CleanupTests(unittest.TestCase):
    def test_newest_import_backup_survives_older_go(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            old = home / "import-backup-old"
            old.mkdir()
            (old / "index.db").write_bytes(b"old")
            _old(old)
            newest = home / "import-backup"
            newest.mkdir()
            (newest / "index.db").write_bytes(b"new")
            done = cleanup_stale_artifacts(home)
            self.assertTrue(newest.is_dir(), "the newest backup survives")
            self.assertFalse(old.exists(), "older backups go")
            self.assertTrue(any("removed old backup" in item for item in done))

    def test_stale_part_files_removed_recent_ones_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            stale = home / "background.png.part"
            stale.write_bytes(b"abandoned")
            _old(stale)
            fresh = home / "hotkey.json.part"
            fresh.write_bytes(b"running download")
            cleanup_stale_artifacts(home)
            self.assertFalse(stale.exists())
            self.assertTrue(fresh.exists(), "a download running now keeps its staging")

    def test_updater_temp_dirs_of_dead_pids_are_removed(self):
        temp = Path(tempfile.gettempdir())
        dead = temp / "autofind-update-999999-deadbeef"
        dead.mkdir(exist_ok=True)
        (dead / "update.exe").write_bytes(b"staged")
        _old(dead, seconds=48 * 3600)
        try:
            done = cleanup_stale_artifacts(Path(tempfile.gettempdir()) / "no-home")
            self.assertFalse(dead.exists())
            self.assertTrue(any("autofind-update" in item for item in done))
        finally:
            import shutil

            shutil.rmtree(dead, ignore_errors=True)

    def test_current_pids_temp_dir_is_kept(self):
        temp = Path(tempfile.gettempdir())
        mine = temp / f"autofind-update-{os.getpid()}-cafe"
        mine.mkdir(exist_ok=True)
        _old(mine, seconds=48 * 3600)
        try:
            cleanup_stale_artifacts(Path(tempfile.gettempdir()) / "no-home")
            self.assertTrue(mine.exists(), "this process's stage dir is not touched")
        finally:
            import shutil

            shutil.rmtree(mine, ignore_errors=True)

    def test_cleanup_on_a_missing_home_is_a_no_op(self):
        self.assertEqual(
            cleanup_stale_artifacts(Path(tempfile.gettempdir()) / "autofind-nope-xyz"), []
        )


class ResetTests(unittest.TestCase):
    def test_reset_keeps_packs_and_wipes_user_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "index.db").write_bytes(b"i")
            (home / "models").mkdir()
            (home / "models" / "e5-small").mkdir()
            (home / "models" / "e5-small" / "model.onnx").write_bytes(b"m")
            (home / "autofind.log").write_text("l")
            plan = reset_plan(home)
            self.assertEqual(plan.keep, ["models"])
            self.assertIn("index.db", plan.remove)
            removed, failed = apply_reset(home)
            self.assertTrue((home / "models" / "e5-small" / "model.onnx").is_file())
            self.assertFalse((home / "index.db").exists())
            self.assertEqual(failed, 0)
            self.assertGreaterEqual(removed, 2)

    def test_reset_with_models_removes_every_pack(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            for pack in PACK_DIRS:
                (home / pack).mkdir()
            (home / "index.db").write_bytes(b"i")
            removed, _ = apply_reset(home, remove_packs=True)
            self.assertFalse((home / "models").exists())
            self.assertFalse((home / "ocr-indra").exists())

    def test_reset_on_a_missing_home_does_nothing(self):
        removed, failed = apply_reset(Path(tempfile.gettempdir()) / "autofind-nope-xyz")
        self.assertEqual((removed, failed), (0, 0))


if __name__ == "__main__":
    unittest.main()
