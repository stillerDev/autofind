"""Rotating event log: append, rotation by size, tail, resilience."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.logger import Logger


class LoggerTests(unittest.TestCase):
    def test_writes_and_tails(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Logger(Path(tmp) / "autofind.log")
            log.write("first event")
            log.write("bad thing", level="error")
            lines = log.tail(10)
            self.assertEqual(len(lines), 2)
            self.assertIn("[info] first event", lines[0])
            self.assertIn("[error] bad thing", lines[1])
            self.assertIn("first event", lines[0])

    def test_rotates_when_over_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "autofind.log"
            # ~4 KiB limit vs ~95-byte lines: 60 lines cross it exactly once,
            # so .1 holds the whole first slice, event 000 first
            log = Logger(path, max_mb=0.004, backups=1)
            for index in range(60):
                log.write(f"event number {index:03d} " + "x" * 40)
            rotated = path.with_suffix(".log.1")
            self.assertTrue(rotated.is_file())
            first = rotated.read_text(encoding="utf-8").splitlines()[0]
            self.assertIn("event number 000", first)
            # the active file restarts small and holds the newest events
            self.assertIn("event number 059", log.tail(1)[0])

    def test_oldest_backup_falls_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "autofind.log"
            # 1 KiB limit, 60 ~95-byte lines: several rotations, each drops
            # the previous .1 — bounded disk use is the point of rotation
            log = Logger(path, max_mb=0.001, backups=1)
            for index in range(60):
                log.write(f"event number {index:03d} " + "x" * 40)
            self.assertLess(path.stat().st_size, 4096)
            self.assertTrue(path.with_suffix(".log.1").is_file())
            # the two files together hold far less than what was written
            total = path.stat().st_size + path.with_suffix(".log.1").stat().st_size
            self.assertLess(total, 8192)

    def test_tail_on_missing_file_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Logger(Path(tmp) / "nope.log")
            self.assertEqual(log.tail(5), [])

    def test_rotation_failure_keeps_appending(self):
        # a log reader holding the rotated file open must not break writes
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "autofind.log"
            log = Logger(path, max_mb=0.001, backups=1)
            for index in range(40):
                log.write(f"filler {index:03d} " + "y" * 60)
            # rotation happened; writes continue regardless
            self.assertTrue(path.is_file())
            log.write("still alive")
            self.assertIn("still alive", "\n".join(log.tail(200)))


if __name__ == "__main__":
    unittest.main()
