"""CLI export: query results as csv/json, matching what the screen shows."""

from __future__ import annotations

import csv
import io
import json
import tempfile
import unittest
from pathlib import Path

from core.cli import main
from core.config import IndexConfig
from core.embed import HashingEmbedder
from core.indexer import run_index
from core.store import Store


class CliExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name) / "corpus"
        root.mkdir()
        (root / "notes.md").write_text(
            "экспорт результатов: hybrid search по файлам", encoding="utf-8"
        )
        (root / "tool.py").write_text("def run():\n    return 1\n", encoding="utf-8")
        cls.db = Path(cls.tmp.name) / "index.db"
        embedder = HashingEmbedder()
        store = Store(cls.db, embedder.name, embedder.dim)
        run_index([root], IndexConfig(home=Path(cls.tmp.name) / "home"), store, embedder)
        store.close()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _main(self, *argv: str) -> int:
        return main(["--db", str(self.db), *argv])

    def test_csv_export_round_trips_through_the_csv_module(self):
        # csv text goes to the raw byte buffer; write to a file to capture it
        out_path = Path(self.tmp.name) / "out.csv"
        code = self._main("export", "hybrid search", "--out", str(out_path))
        self.assertEqual(code, 0)
        raw = out_path.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))  # the Excel BOM
        rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))
        self.assertEqual(
            rows[0],
            ["name", "path", "kind", "size", "modified", "score", "opens", "tags"],
        )
        self.assertGreaterEqual(len(rows), 2)
        self.assertIn("notes.md", rows[1][0])

    def test_json_export_keeps_typed_fields(self):
        out_path = Path(self.tmp.name) / "out.json"
        self.assertEqual(self._main("export", "hybrid", "--format", "json",
                                    "--out", str(out_path)), 0)
        payload = json.loads(out_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["query"], "hybrid")
        self.assertGreater(payload["exported"], 0)
        hit = payload["hits"][0]
        for field in ("file_id", "name", "path", "kind", "size", "mtime", "score"):
            self.assertIn(field, hit)

    def test_export_refuses_without_an_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = main(["--db", str(Path(tmp) / "nope.db"), "export", "x"])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
