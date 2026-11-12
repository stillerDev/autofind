"""Idle-only indexing: cycles wait until the keyboard and mouse are quiet."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from core.idle import IdleGate, _idle_from
from core.watch import Watcher


class IdleMathTests(unittest.TestCase):
    def test_tick_delta_across_a_wrap(self):
        # GetTickCount wraps after 49 days: last=100 ticks before wrap,
        # now=300 after it, the true delta is 200 ms, not a negative number
        self.assertAlmostEqual(_idle_from(4_294_967_200, 300), 0.396, delta=1e-6)
        self.assertAlmostEqual(_idle_from(0, 1500), 1.5, delta=1e-6)


class GateTests(unittest.TestCase):
    def test_open_when_detection_is_unavailable(self):
        # no GetLastInputInfo on this platform (or it failed): never block
        gate = IdleGate(min_idle=30, seconds_idle_fn=lambda: None)
        self.assertTrue(gate.ready())

    def test_waits_until_min_idle(self):
        gate = IdleGate(min_idle=30, seconds_idle_fn=lambda: 12.0)
        self.assertFalse(gate.ready())
        gate2 = IdleGate(min_idle=30, seconds_idle_fn=lambda: 31.0)
        self.assertTrue(gate2.ready())

    def test_seconds_idle_answers_on_windows(self):
        # the real call: on a test runner it usually reports a small number
        seconds = __import__("core.idle", fromlist=["seconds_idle"]).seconds_idle()
        if seconds is None:
            self.skipTest("GetLastInputInfo unavailable")
        self.assertGreaterEqual(seconds, 0.0)
        self.assertLess(seconds, 60 * 60 * 24)  # a fresh runner is not idle for a day


class WatcherIdleTests(unittest.TestCase):
    def _watcher(self, tmp: str, idle_fn) -> Watcher:
        config = __import__("core.config", fromlist=["IndexConfig"]).IndexConfig(
            home=Path(tmp) / "home"
        )
        return Watcher(
            [Path(tmp) / "corpus"], config, Path(tmp) / "index.db",
            interval=0.05, settle=0.05,
            idle_only=True, idle_min=30,
        ), idle_fn

    def test_active_user_defers_cycles(self):
        with tempfile.TemporaryDirectory() as tmp:
            corpus = Path(tmp) / "corpus"
            corpus.mkdir()
            (corpus / "a.txt").write_text("hello\n", encoding="utf-8")
            watcher = Watcher(
                [corpus], __import__("core.config", fromlist=["IndexConfig"]).IndexConfig(
                    home=Path(tmp) / "home"),
                Path(tmp) / "index.db",
                interval=0.05, settle=0.05,
                idle_only=True, idle_min=30,
            )
            # the user has been typing for the whole run: no cycle may index
            watcher._idle_gate = IdleGate(min_idle=30, seconds_idle_fn=lambda: 1.0)
            watcher.start()
            time.sleep(0.4)
            watcher.stop()
            self.assertEqual(watcher.state.cycles, 0)
            self.assertEqual(watcher.state.added, 0)

    def test_idle_user_lets_cycles_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            corpus = Path(tmp) / "corpus"
            corpus.mkdir()
            (corpus / "a.txt").write_text("hello\n", encoding="utf-8")
            watcher = Watcher(
                [corpus], __import__("core.config", fromlist=["IndexConfig"]).IndexConfig(
                    home=Path(tmp) / "home"),
                Path(tmp) / "index.db",
                interval=0.05, settle=0.05,
                idle_only=True, idle_min=30,
            )
            watcher._idle_gate = IdleGate(min_idle=30, seconds_idle_fn=lambda: 60.0)
            watcher.start()
            deadline = time.time() + 5
            while time.time() < deadline and watcher.state.cycles == 0:
                time.sleep(0.05)
            watcher.stop()
            self.assertGreaterEqual(watcher.state.cycles, 1)

    def test_detection_failure_never_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            corpus = Path(tmp) / "corpus"
            corpus.mkdir()
            (corpus / "a.txt").write_text("hello\n", encoding="utf-8")
            watcher = Watcher(
                [corpus], __import__("core.config", fromlist=["IndexConfig"]).IndexConfig(
                    home=Path(tmp) / "home"),
                Path(tmp) / "index.db",
                interval=0.05, settle=0.05,
                idle_only=True, idle_min=30,
            )
            watcher._idle_gate = IdleGate(min_idle=30, seconds_idle_fn=lambda: None)
            watcher.start()
            deadline = time.time() + 5
            while time.time() < deadline and watcher.state.cycles == 0:
                time.sleep(0.05)
            watcher.stop()
            self.assertGreaterEqual(watcher.state.cycles, 1)


if __name__ == "__main__":
    unittest.main()
