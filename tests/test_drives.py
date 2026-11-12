"""Network-drive guard: remote roots are refused unless explicitly allowed."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from core.drives import drive_type, remote_roots
from core.watch import Watcher


def _cfg(tmp: str):
    from core.config import IndexConfig

    return IndexConfig(home=Path(tmp) / "home")


class DriveTypeTests(unittest.TestCase):
    def test_local_temp_is_fixed(self):
        self.assertEqual(drive_type(tempfile.gettempdir()), "fixed")

    def test_drive_root_extraction(self):
        from core.drives import _drive_root

        self.assertEqual(_drive_root(r"C:\work\a.txt"), "C:\\")
        # a UNC share root is a valid GetDriveTypeW argument (DRIVE_REMOTE)
        self.assertEqual(_drive_root(r"\\srv\share\a.txt"), "\\\\srv\\share\\")
        self.assertEqual(_drive_root("relative/path.txt"), None)

    def test_remote_roots_uses_the_injected_type(self):
        roots = [r"C:\\local", r"\\\\srv\\share", r"D:\\data"]
        remote = remote_roots(
            roots, type_fn=lambda p: "remote" if str(p).startswith("\\\\") else "fixed"
        )
        self.assertEqual(remote, ["\\\\\\\\srv\\\\share"])


class WatcherRemoteGuardTests(unittest.TestCase):
    def test_remote_root_refused_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            watcher = Watcher(
                [r"\\\\srv\\share"], _cfg(tmp), Path(tmp) / "index.db",
            )
            # inject a fake drive type: no real network drive in CI
            watcher._drive_type = lambda p: "remote" if str(p).startswith("\\\\") else "fixed"
            watcher.run()
            self.assertIn("network drives", watcher.state.last_error or "")

    def test_remote_root_allowed_with_explicit_opt_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            watcher = Watcher(
                [r"\\srv\share"], _cfg(tmp), Path(tmp) / "index.db",
                allow_remote=True,
            )
            watcher._drive_type = lambda p: "remote" if str(p).startswith("\\\\") else "fixed"
            # run() enters the live loop, so exercise it in a thread and stop
            import threading

            thread = threading.Thread(target=watcher.run, daemon=True)
            thread.start()
            time.sleep(0.4)
            watcher.stop()
            thread.join(timeout=3)
            # the share does not exist, so streaming/polling fails cleanly,
            # but the guard itself must not be the reason it stopped
            self.assertNotIn("network drives", watcher.state.last_error or "")

    def test_local_root_never_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            corpus = Path(tmp) / "corpus"
            corpus.mkdir()
            (corpus / "a.txt").write_text("hello\n", encoding="utf-8")
            watcher = Watcher([corpus], _cfg(tmp), Path(tmp) / "index.db")
            watcher.start()
            deadline = time.time() + 5
            while time.time() < deadline and watcher.state.cycles == 0:
                time.sleep(0.05)
            watcher.stop()
            self.assertIsNone(watcher.state.last_error)
            self.assertGreaterEqual(watcher.state.cycles, 1)


if __name__ == "__main__":
    unittest.main()
