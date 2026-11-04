"""The CJK bigram table and the trigram typo tier: transforms, queries, e2e."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.cjk import cjk_bigrams, cjk_query_phrase, has_cjk
from core.config import IndexConfig
from core.embed import HashingEmbedder
from core.query import understand
from core.search import build_match_query, build_tri_query, search_page
from core.store import Store


class CjkTransformTests(unittest.TestCase):
    def test_runs_become_overlapping_bigrams(self):
        self.assertEqual(cjk_bigrams("数据库索引"), "数据 据库 库索 索引")

    def test_latin_text_passes_through_untouched(self):
        text = "plain english text"
        self.assertEqual(cjk_bigrams(text), text)

    def test_mixed_text_keeps_both_parts(self):
        # A two-character run's bigram is itself, so use three to see the split.
        self.assertEqual(cjk_bigrams("CUDA数据库"), "CUDA 数据 据库")

    def test_single_character_is_its_own_token(self):
        self.assertEqual(cjk_bigrams("光"), "光")

    def test_query_phrase_is_ordered_bigrams(self):
        self.assertEqual(cjk_query_phrase("数据库索引"), "数据 据库 库索 索引")
        self.assertIsNone(cjk_query_phrase("光"))

    def test_has_cjk(self):
        self.assertTrue(has_cjk("索引"))
        self.assertFalse(has_cjk("invoice"))


class MatchQueryTests(unittest.TestCase):
    def test_cjk_run_becomes_a_quoted_phrase(self):
        self.assertIn('"数据 据库"', build_match_query("数据库"))

    def test_plain_terms_keep_the_prefix_star(self):
        self.assertIn('"invoice"*', build_match_query("invoice"))

    def test_mixed_token_splits_into_term_and_phrase(self):
        query = build_match_query("CUDA数据")
        self.assertIn('"cuda"*', query)
        self.assertIn('"数据"', query)


class TriQueryTests(unittest.TestCase):
    def test_long_terms_break_into_grams(self):
        query = build_tri_query("invoive")
        for gram in ("inv", "nvo", "voi", "oiv", "ive"):
            self.assertIn(f'"{gram}"', query)

    def test_short_terms_are_skipped(self):
        self.assertIsNone(build_tri_query("ab cd"))


class EndToEndTests(unittest.TestCase):
    def _store(self, tmp: str) -> Store:
        config = IndexConfig()
        config.home = Path(tmp)
        return Store(Path(tmp) / "index.db", "hashing-384", 384)

    def test_cjk_query_finds_what_bm25_cannot(self):
        embedder = HashingEmbedder()
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            try:
                file_id = store.upsert_file("C:\\notes\\db.txt", 100, 1.0, "a" * 64, "text")
                store.replace_chunks(file_id, ["索引加速查询 " * 4], None)
                store.commit()
                page = search_page(store, embedder, "索引", limit=5, offset=0, sort=None,
                                   parsed=understand("索引"), usage={}, applied=False)
                self.assertEqual([hit.name for hit in page.hits], ["db.txt"])
                self.assertIn("cjk", page.hits[0].channels)
            finally:
                store.close()

    def test_typo_query_reaches_the_file_via_trigram(self):
        embedder = HashingEmbedder()
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            try:
                file_id = store.upsert_file("C:\\notes\\bill.txt", 100, 1.0, "b" * 64, "text")
                store.replace_chunks(file_id, ["The invoice for the hosting service"], None)
                store.commit()
                page = search_page(store, embedder, "invoive hosting", limit=5, offset=0,
                                   sort=None, parsed=understand("invoive hosting"),
                                   usage={}, applied=False)
                self.assertEqual([hit.name for hit in page.hits], ["bill.txt"])
                self.assertIn("tri", page.hits[0].channels)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
