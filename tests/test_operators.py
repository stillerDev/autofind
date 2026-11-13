"""The ocr: / from: / subject: operators.

Three filters over columns kensaku does not serve: text read off pixels, and
the two mail headers people actually remember. The parsers' job is to route
them into the scope intersection; the tests pin both the routing and the
scope SQL.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.config import IndexConfig
from core.embed import HashingEmbedder
from core.indexer import run_index
from core.query import understand
from core.store import Store


class OperatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name) / "corpus"
        root.mkdir()
        (root / "letter.eml").write_text(
            "From: Anna Petrova <anna@corp.ru>\r\n"
            "Subject: Бюджет за квартал\r\n"
            "To: team@corp.ru\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "\r\n"
            "Приложил таблицу с цифрами.\r\n",
            encoding="utf-8",
        )
        (root / "notes.md").write_text(
            "обычная заметка про покупки " * 5, encoding="utf-8"
        )
        cls.db = Path(cls.tmp.name) / "index.db"
        embedder = HashingEmbedder()
        store = Store(cls.db, embedder.name, embedder.dim)
        run_index([root], IndexConfig(home=Path(cls.tmp.name) / "home"), store, embedder)
        # a screenshot the OCR chain read
        letter_id = store.conn.execute(
            "SELECT id FROM files WHERE name = 'letter.eml'").fetchone()[0]
        import numpy as np
        store.upsert_image(
            path=str(root / "shot.png"), file_id=letter_id,
            sha256="shot", width=800, height=600, size=1024,
            mtime=0.0, thumb=b"\x89PNG", ocr_text="invoice total 4400",
            ocr_engine="test", vector=np.zeros(384, dtype=np.float32),
        )
        store.conn.commit()
        store.close()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _store(self):
        emb = HashingEmbedder()
        return Store(self.db, emb.name, emb.dim, readonly=True)

    def test_from_operator_routes_and_matches(self):
        parsed = understand("from:anna")
        self.assertEqual(parsed.email_from, ["anna"])
        store = self._store()
        try:
            scope = store.email_scope(parsed.email_from, parsed.email_subject)
        finally:
            store.close()
        self.assertEqual(len(scope), 1)  # only the .eml

    def test_subject_operator_matches_cyrillic_header(self):
        parsed = understand("subject:бюджет")
        self.assertEqual(parsed.email_subject, ["бюджет"])
        store = self._store()
        try:
            scope = store.email_scope(parsed.email_from, parsed.email_subject)
        finally:
            store.close()
        self.assertEqual(len(scope), 1)

    def test_subject_misses_when_wrong(self):
        store = self._store()
        try:
            scope = store.email_scope([], ["несуществующая тема"])
        finally:
            store.close()
        self.assertEqual(scope, set())

    def test_ocr_operator_reads_pixels_text(self):
        parsed = understand("ocr:invoice")
        self.assertEqual(parsed.ocr_terms, ["invoice"])
        store = self._store()
        try:
            scope = store.ocr_scope(parsed.ocr_terms)
        finally:
            store.close()
        self.assertEqual(len(scope), 1)

    def test_ocr_scope_requires_all_terms(self):
        store = self._store()
        try:
            hit = store.ocr_scope(["invoice"])
            miss = store.ocr_scope(["invoice", "невидимка"])
        finally:
            store.close()
        self.assertTrue(hit)
        self.assertEqual(miss, set())

    def test_operators_count_as_filters(self):
        self.assertTrue(understand("from:anna").has_filters)
        self.assertTrue(understand("ocr:invoice").has_filters)
        self.assertTrue(understand("subject:x").has_filters)
        self.assertFalse(understand("plain words").has_filters)


if __name__ == "__main__":
    unittest.main()
