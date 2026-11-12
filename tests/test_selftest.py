"""Startup self-test: catches corruption, orphan spans and hash mismatches."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.config import IndexConfig
from core.doctor import self_test
from core.embed import HashingEmbedder
from core.indexer import run_index
from core.store import Store


def _indexed(tmp: str) -> Path:
    root = Path(tmp) / "corpus"
    root.mkdir()
    (root / "notes.md").write_text(
        "проверка целостности: self-test считает чанки и хеши", encoding="utf-8"
    )
    db = Path(tmp) / "index.db"
    embedder = HashingEmbedder()
    store = Store(db, embedder.name, embedder.dim)
    run_index([root], IndexConfig(home=Path(tmp) / "home"), store, embedder)
    store.close()
    return db


class SelfTestTests(unittest.TestCase):
    def test_healthy_index_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _indexed(tmp)
            result = self_test(db)
            self.assertTrue(result["ok"], result["problems"])
            self.assertGreaterEqual(result["checks"]["hashes_verified"], 1)
            self.assertEqual(result["checks"]["quick_check"], "ok")

    def test_missing_index_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self_test(Path(tmp) / "nope.db")
            self.assertFalse(result["ok"])
            self.assertIn("no index file", result["problems"][0])

    def test_orphan_chunks_are_named(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _indexed(tmp)
            embedder = HashingEmbedder()
            store = Store(db, embedder.name, embedder.dim)
            # a chunk whose file vanished: the structural check exists for this
            # (legacy dbs had no enforced FK, so the state is constructible)
            store.conn.execute("PRAGMA foreign_keys = OFF")
            file_id = store.conn.execute("SELECT id FROM files LIMIT 1").fetchone()[0]
            store.conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
            store.conn.commit()
            store.close()
            result = self_test(db)
            self.assertFalse(result["ok"])
            self.assertTrue(any("chunks point at deleted files" in p for p in result["problems"]))

    def test_tampered_text_fails_the_hash_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _indexed(tmp)
            embedder = HashingEmbedder()
            store = Store(db, embedder.name, embedder.dim)
            # rewrite the stored bytes without updating the hash key
            row = store.conn.execute(
                "SELECT hash, raw_len, block_size, offsets, data FROM file_texts LIMIT 1"
            ).fetchone()
            import zlib

            fake = zlib.compress(b"tampered content", 6)
            store.conn.execute(
                "UPDATE file_texts SET raw_len = ?, data = ? WHERE hash = ?",
                (len("tampered content"), fake, row["hash"]),
            )
            store.conn.commit()
            store.close()
            result = self_test(db)
            self.assertFalse(result["ok"])
            self.assertTrue(
                any("no longer matches its hash" in p for p in result["problems"]),
                result["problems"],
            )


if __name__ == "__main__":
    unittest.main()
