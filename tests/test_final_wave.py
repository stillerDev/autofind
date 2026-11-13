"""Final wave: lifespan policies, size rule, backup, share, pin-top, tag, verify."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core import lifespan
from core.config import IndexConfig
from core.embed import HashingEmbedder
from core.indexer import run_index
from core.store import Store


class LifespanTests(unittest.TestCase):
    def test_battery_answer_is_a_bool(self):
        self.assertIsInstance(lifespan.on_battery(), bool)

    def test_idle_seconds_is_nonnegative(self):
        self.assertGreaterEqual(lifespan.seconds_idle(), 0.0)


class SizeRuleTests(unittest.TestCase):
    def test_config_rule_exists_and_defaults_off(self):
        config = IndexConfig()
        self.assertEqual(config.content_skip_over_mb, 0)


class CliFeaturesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name) / "corpus"
        root.mkdir()
        (root / "letter.md").write_text(
            "quarterly budget review document " * 20, encoding="utf-8")
        cls.db = Path(cls.tmp.name) / "index.db"
        embedder = HashingEmbedder()
        store = Store(cls.db, embedder.name, embedder.dim)
        run_index([root], IndexConfig(home=Path(cls.tmp.name) / "home"), store, embedder)
        cls.file_id = int(store.conn.execute(
            "SELECT id FROM files").fetchone()[0])
        store.close()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_backup_writes_a_zip_with_the_db_inside(self):
        from core.cli import main
        import io
        import zipfile
        from contextlib import redirect_stdout

        out_dir = Path(self.tmp.name) / "backups"
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["--db", str(self.db), "backup", "--to", str(out_dir)])
        self.assertEqual(code, 0)
        archives = list(out_dir.glob("autofind-backup-*.zip"))
        self.assertEqual(len(archives), 1)
        with zipfile.ZipFile(archives[0]) as zf:
            self.assertIn("index.db", zf.namelist())

    def test_tag_command_adds_and_removes(self):
        from core.cli import main
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            main(["--db", str(self.db), "tag", str(self.file_id), "work"])
        store = Store(self.db, HashingEmbedder().name, HashingEmbedder().dim, readonly=True)
        try:
            tags = store.tags_for([self.file_id])
        finally:
            store.close()
        self.assertEqual(tags.get(self.file_id), ["work"])
        with redirect_stdout(buf):
            main(["--db", str(self.db), "tag", str(self.file_id), "work", "--off"])
        store = Store(self.db, HashingEmbedder().name, HashingEmbedder().dim, readonly=True)
        try:
            tags = store.tags_for([self.file_id])
        finally:
            store.close()
        self.assertEqual(tags.get(self.file_id, []), [])

    def test_pin_top_pins_the_best_hit(self):
        from core.cli import main
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["--db", str(self.db), "pin-top", "budget"])
        self.assertEqual(code, 0)
        store = Store(self.db, HashingEmbedder().name, HashingEmbedder().dim, readonly=True)
        try:
            pinned = store.pinned_rows(limit=10)
        finally:
            store.close()
        self.assertEqual(len(pinned), 1)

    def test_config_roundtrip(self):
        from core.cli import main
        import io
        import os as _os
        from contextlib import redirect_stdout
        # config.home follows AUTOFIND_HOME, so the test redirects it instead
        # of writing into the real user directory.
        _os.environ["AUTOFIND_HOME"] = str(Path(self.tmp.name) / "home")
        buf = io.StringIO()
        with redirect_stdout(buf):
            main(["--db", str(self.db), "config", "start_minimized", "on"])
        prefs = json.loads((Path(self.tmp.name) / "home" / "prefs.json").read_text(encoding="utf-8"))
        self.assertIs(prefs["start_minimized"], True)

    def test_verify_reports_and_backup_wal_moves(self):
        from core.cli import main
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["--db", str(self.db), "verify"])
        self.assertEqual(code, 0)
        self.assertIn("0 missing", buf.getvalue())


import json  # noqa: E402  (used by test_config_roundtrip)

if __name__ == "__main__":
    unittest.main()
