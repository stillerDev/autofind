"""The five corpus finders: big, stale, dupes, dead, top.

These are the questions the filesystem answers only by being walked again.
Here the index already holds every column the answer needs, so each finder is
one SQL statement plus a guard — the tests pin the guards, not the SQL.
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from core import finders
from core.config import IndexConfig
from core.embed import HashingEmbedder
from core.indexer import run_index
from core.store import Store


class FinderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name) / "corpus"
        root.mkdir()
        big = root / "huge.bin"
        big.write_bytes(b"Z" * (2 * 1048576))
        tiny = root / "tiny.txt"
        tiny.write_bytes(b"A" * 100)
        cls.copy = root / "copy.bin"
        cls.copy.write_bytes(b"Z" * (2 * 1048576))  # same bytes as huge.bin
        old = root / "ancient.md"
        old.write_text("ancient notes " * 8, encoding="utf-8")
        ancient = time.time() - 400 * 86400
        os.utime(old, (ancient, ancient))
        cls.db = Path(cls.tmp.name) / "index.db"
        embedder = HashingEmbedder()
        store = Store(cls.db, embedder.name, embedder.dim)
        run_index([root], IndexConfig(home=Path(cls.tmp.name) / "home"), store, embedder)
        store.close()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _store(self):
        embedder = HashingEmbedder()
        return Store(self.db, embedder.name, embedder.dim, readonly=True)

    def test_big_sorts_by_size_and_respects_the_floor(self):
        store = self._store()
        try:
            rows = finders.biggest(store.conn, min_mb=1.0)
        finally:
            store.close()
        self.assertEqual(sorted(r.name for r in rows), ["copy.bin", "huge.bin"])
        self.assertTrue(all(r.size >= 1048576 for r in rows))
        store = self._store()
        try:
            rows = finders.biggest(store.conn, min_mb=5.0)
        finally:
            store.close()
        self.assertEqual(rows, [])

    def test_stale_finds_only_old_files(self):
        store = self._store()
        try:
            rows = finders.stale(store.conn, days=365)
        finally:
            store.close()
        self.assertEqual([r.name for r in rows], ["ancient.md"])
        store = self._store()
        try:
            rows = finders.stale(store.conn, days=500)
        finally:
            store.close()
        # a 400-day-old file is not stale for a 500-day window
        self.assertNotIn("ancient.md", [r.name for r in rows])

    def test_dupes_groups_by_content_hash(self):
        store = self._store()
        try:
            rows = finders.duplicates(store.conn)
        finally:
            store.close()
        names = {r.name for r in rows}
        self.assertEqual(names, {"huge.bin", "copy.bin"})
        self.assertEqual(rows[0].score, 2)  # two copies of the same bytes

    def test_dead_lists_missing_paths_and_prunes_them(self):
        # a file indexed but then deleted from disk
        gone = Path(self.tmp.name) / "corpus" / "doomed.txt"
        gone.write_text("delete me " * 8, encoding="utf-8")
        embedder = HashingEmbedder()
        store = Store(self.db, embedder.name, embedder.dim)
        run_index([gone.parent], IndexConfig(home=Path(self.tmp.name) / "home"),
                  store, embedder)
        before = store.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        store.close()
        gone.unlink()

        store = Store(self.db, embedder.name, embedder.dim, readonly=True)
        try:
            rows = finders.dead_paths(store.conn)
            self.assertIn("doomed.txt", [r.name for r in rows])
        finally:
            store.close()

        store = Store(self.db, embedder.name, embedder.dim)
        try:
            removed = finders.prune_dead(store, rows)
            after = store.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        finally:
            store.close()
        self.assertEqual(removed, 1)
        self.assertEqual(after, before - 1)

    def test_top_ranks_by_opens(self):
        store = Store(self.db, embedder_name := HashingEmbedder().name,
                      embedder_dim := HashingEmbedder().dim)
        try:
            row = store.conn.execute(
                "SELECT id FROM files WHERE name = 'huge.bin'").fetchone()
            store.conn.execute(
                "INSERT INTO usage(file_id, opens, last_open) VALUES (?, 7, ?)",
                (row[0], time.time()))
            store.conn.commit()
        finally:
            store.close()
        store = self._store()
        try:
            rows = finders.most_opened(store.conn)
        finally:
            store.close()
        self.assertEqual(rows[0].name, "huge.bin")
        self.assertEqual(rows[0].score, 7)


if __name__ == "__main__":
    unittest.main()
