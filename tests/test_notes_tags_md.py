"""The human layer: notes on files, coloured tags, markdown export.

A note answers "why did I keep this?", a coloured tag makes the drawer chip
readable at a glance, and markdown export is how results travel into chat or
docs. All three live beside the index; none of them exist in kensaku.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.config import IndexConfig
from core.embed import HashingEmbedder
from core.indexer import run_index
from core.store import Store


class NotesTagsMdTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name) / "corpus"
        root.mkdir()
        (root / "kept.md").write_text("важный документ про бюджет " * 10, encoding="utf-8")
        cls.db = Path(cls.tmp.name) / "index.db"
        embedder = HashingEmbedder()
        store = Store(cls.db, embedder.name, embedder.dim)
        run_index([root], IndexConfig(home=Path(cls.tmp.name) / "home"), store, embedder)
        cls.file_id = int(store.conn.execute(
            "SELECT id FROM files WHERE name = 'kept.md'").fetchone()[0])
        store.close()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _store(self, readonly=True):
        emb = HashingEmbedder()
        return Store(self.db, emb.name, emb.dim, readonly=readonly)

    def test_note_roundtrip_and_clear(self):
        store = self._store(False)
        try:
            self.assertTrue(store.set_note(self.file_id, "держу до конца квартала"))
            self.assertEqual(store.notes_for([self.file_id])[self.file_id],
                             "держу до конца квартала")
            # replace
            store.set_note(self.file_id, "новая причина")
            self.assertEqual(store.notes_for([self.file_id])[self.file_id], "новая причина")
            # clear
            store.set_note(self.file_id, None)
            self.assertEqual(store.notes_for([self.file_id]), {})
        finally:
            store.close()

    def test_note_text_is_whitespace_collapsed(self):
        store = self._store(False)
        try:
            store.set_note(self.file_id, "  много    пробелов  ")
            self.assertEqual(store.notes_for([self.file_id])[self.file_id],
                             "много пробелов")
        finally:
            store.close()

    def test_tag_color_roundtrip_and_removal(self):
        store = self._store(False)
        try:
            self.assertTrue(store.set_tag_color("работа", "#ff5500"))
            self.assertEqual(store.tag_colors(), {"работа": "#ff5500"})
            self.assertTrue(store.set_tag_color("работа", None))
            self.assertEqual(store.tag_colors(), {})
            # a wrong colour literal is rejected, not stored
            self.assertFalse(store.set_tag_color("x", "red"))
        finally:
            store.close()

    def test_markdown_export_has_headers_and_snippets(self):
        from core.cli import main
        import io
        from contextlib import redirect_stdout
        out_path = Path(self.tmp.name) / "out.md"
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["--db", str(self.db), "export", "бюджет",
                         "--format", "md", "--out", str(out_path)])
        self.assertEqual(code, 0)
        text = out_path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# AutoFind: бюджет"))
        self.assertIn("`", text)          # path in backticks
        self.assertIn("kept.md", text)    # the hit itself


if __name__ == "__main__":
    unittest.main()
