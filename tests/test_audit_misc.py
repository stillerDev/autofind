"""Regression tests for the audit fixes that had no direct coverage.

Small and dependency-free on purpose: these check the mechanism, not an OCR
model or a live server.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from core.evaluate import matches as eval_matches
from core.importers import read_efu
from core.maintenance import CleanupConfig
from core.ocr import _patience, reset_fallbacks
from core.ocr.indra import CORE_THRESHOLD, Engine, decode_lines
from core.transfer import export_index


class PatienceTests(unittest.TestCase):
    def test_each_thread_has_its_own_budget_and_reset_reaches_all(self):
        reset_fallbacks()
        seen: list[object] = []
        barrier = threading.Barrier(2)

        def worker() -> None:
            budget = _patience()
            seen.append(budget)
            barrier.wait(timeout=5)
            for _ in range(budget.limit):
                budget.note(False)
            self.assertTrue(budget.spent())
            barrier.wait(timeout=5)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len({id(item) for item in seen}), 2)
        reset_fallbacks()
        for item in seen:
            self.assertFalse(item.spent())


class IndraFixTests(unittest.TestCase):
    def test_threshold_ignores_noise_logits(self):
        self.assertEqual(CORE_THRESHOLD, 0.5)
        out = np.full((3, 6, 8), -10.0, dtype=np.float32)
        out[0, 1:4, 2:5] = 0.1  # positive but below threshold
        self.assertEqual(decode_lines(out, 6, 8), [])
        out[0, 1:4, 2:5] = 0.9
        self.assertEqual(len(decode_lines(out, 6, 8)), 1)

    def test_meta_without_base_vocab_raises_value_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "meta.json").write_text(
                json.dumps({"script_groups": [], "rec_height": 32}), encoding="utf-8"
            )
            with self.assertRaises(ValueError) as caught:
                Engine.load(directory)
            self.assertIn("vocab", str(caught.exception))


class EvaluateFixTests(unittest.TestCase):
    def test_substring_does_not_inflate_recall_but_suffix_labels_still_work(self):
        self.assertFalse(eval_matches("C:/pics/myreport.docx", "report.doc"))
        self.assertFalse(eval_matches("C:/docs/report.docx", "other.md"))
        self.assertTrue(eval_matches("C:/docs/plan.docx", "plan.docx"))
        self.assertTrue(eval_matches("C:/home/me/docs/plan.docx", "docs/plan"))


class ImporterFixTests(unittest.TestCase):
    def test_rfc2822_date_is_parsed(self):
        text = (
            "Filename,Size,Date Modified\n"
            'C:\\work\\mail.txt,10,"Mon, 12 Nov 2026 18:24:00 +0000"\n'
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "export.efu"
            path.write_text(text, encoding="utf-8")
            entries = read_efu(path)
        self.assertEqual(len(entries), 1)
        self.assertAlmostEqual(entries[0].mtime, 1794507840.0, delta=60)


class SqliteUriTests(unittest.TestCase):
    def test_export_with_hash_in_the_path(self):
        # '?' is illegal in a Windows filename; '#' is the character that used
        # to truncate the raw sqlite URI.
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "index#1"
            directory.mkdir()
            db = directory / "index.db"
            conn = sqlite3.connect(str(db))
            for ddl in (
                "CREATE TABLE meta (key TEXT, value TEXT)",
                "CREATE TABLE file_vectors (dim INTEGER)",
                "CREATE TABLE files (id INTEGER)",
                "CREATE TABLE chunks (id INTEGER)",
            ):
                conn.execute(ddl)
            conn.commit()
            conn.close()
            manifest = export_index(db, directory / "export.zip")
            self.assertEqual(manifest["files"], 0)
            self.assertIn("index.db", manifest["entries"])

    def test_migrate_reads_a_hashed_path(self):
        from core.migrate import _read_sqlite

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "index#2.db"
            conn = sqlite3.connect(str(path))
            conn.execute("CREATE TABLE pins (path TEXT)")
            conn.execute("INSERT INTO pins VALUES ('C:\\work\\a.txt')")
            conn.commit()
            conn.close()
            source = _read_sqlite(path)
        self.assertEqual(source.kind, "sqlite")


class MaintenanceConfigTests(unittest.TestCase):
    def test_env_overrides_keep_defaults_for_bad_values(self):
        with mock.patch.dict(os.environ, {"AUTOFIND_GRACE_SECONDS": "12"}):
            self.assertEqual(CleanupConfig.from_env().grace_seconds, 12)
        with mock.patch.dict(os.environ, {"AUTOFIND_GRACE_SECONDS": "nope"}):
            self.assertEqual(CleanupConfig.from_env().grace_seconds, CleanupConfig().grace_seconds)


class ShortcutFixTests(unittest.TestCase):
    def test_remove_checks_supported(self):
        from core import shortcut

        with mock.patch.object(shortcut, "supported", return_value=False):
            with self.assertRaises(RuntimeError):
                shortcut.remove()


class SpecFixTests(unittest.TestCase):
    def test_specs_resolve_crt_dlls_from_system_root(self):
        root = Path(__file__).resolve().parent.parent
        for name in ("afdiag.spec", "afdiag2.spec"):
            text = (root / name).read_text(encoding="utf-8")
            self.assertNotIn("C:/Windows/System32", text)
            self.assertTrue("SystemRoot" in text or "afdiag.spec" in text)


if __name__ == "__main__":
    unittest.main()
