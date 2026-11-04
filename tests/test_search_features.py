"""Search-maturity features: scope pushdown, facets, trigram verification,
did-you-mean suggestions."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.config import IndexConfig
from core.embed import HashingEmbedder
from core.query import understand
from core.search import (
    _suggest_term,
    _verify_tri,
    scoped_file_ids,
    search_page,
)
from core.store import Store


def make_store(tmp: str) -> tuple[Store, HashingEmbedder]:
    config = IndexConfig()
    config.home = Path(tmp)
    embedder = HashingEmbedder()
    store = Store(Path(tmp) / "index.db", embedder.name, embedder.dim)
    return store, embedder


def add_file(store: Store, path: str, text: str, kind: str = "text") -> None:
    file_id = store.upsert_file(path, 100, 1.0, "x" * 64, kind)
    store.replace_chunks(file_id, [text], None)


def run(store: Store, embedder, query: str, **kwargs):
    parsed = kwargs.pop("parsed", None) or understand(query)
    return search_page(
        store, embedder, query, limit=12, offset=0, sort=None,
        parsed=parsed, usage={}, applied=False, **kwargs,
    )


class ScopeTests(unittest.TestCase):
    def test_scope_respects_the_path_boundary(self):
        # `in:"docs"` must not catch `C:\\docs2\\...` — a startswith without a
        # separator check would.
        with tempfile.TemporaryDirectory() as tmp:
            store, _ = make_store(tmp)
            try:
                inside = store.upsert_file("C:\\docs\\report.txt", 10, 1.0, "a" * 64, "text")
                outside = store.upsert_file("C:\\docs2\\other.txt", 10, 1.0, "b" * 64, "text")
                ids = scoped_file_ids(store, ["C:\\docs"])
                self.assertIn(inside, ids)
                self.assertNotIn(outside, ids)
            finally:
                store.close()

    def test_narrow_scope_does_not_lose_the_only_hit(self):
        embedder = HashingEmbedder()
        with tempfile.TemporaryDirectory() as tmp:
            store, embedder = make_store(tmp)
            try:
                # Enough filler files so the narrow folder's file would fall
                # out of a shallow candidate pool without the pushdown.
                for index in range(30):
                    add_file(store, f"C:\\lib\\noise_{index:02d}.txt", "generic filler text")
                add_file(store, "C:\\deep\\nested\\target.txt", "the quarterly budget report")
                store.commit()
                page = run(store, embedder, 'quarterly in:"C:\\deep\\nested"')
                self.assertEqual([hit.name for hit in page.hits], ["target.txt"])
            finally:
                store.close()


class FacetTests(unittest.TestCase):
    def test_facets_count_kinds_over_candidates(self):
        embedder = HashingEmbedder()
        with tempfile.TemporaryDirectory() as tmp:
            store, embedder = make_store(tmp)
            try:
                for index in range(3):
                    add_file(store, f"C:\\lib\\note{index}.txt", "database indexes")
                add_file(store, "C:\\lib\\note.py", "database indexes", kind="code")
                store.commit()
                page = run(store, embedder, "database indexes")
                self.assertEqual(page.facets.get("text"), 3)
                self.assertEqual(page.facets.get("code"), 1)
            finally:
                store.close()


class VerifyTests(unittest.TestCase):
    def test_two_grams_pass_one_gram_fails(self):
        rows = {1: {"text": "The invoice for the hosting service"}}
        # `invoive` shares inv, nvo, voi with invoice — real evidence.
        self.assertEqual(_verify_tri([1], rows, "invoive"), [1])
        # `zebra` shares nothing — the candidate must not surface.
        self.assertEqual(_verify_tri([1], rows, "zebra"), [])

    def test_exact_substring_passes(self):
        rows = {1: {"text": "config for the deploy pipeline"}}
        self.assertEqual(_verify_tri([1], rows, "deploy"), [1])

    def test_suggestion_comes_from_reached_chunks(self):
        rows = {1: {"text": "The invoice for the hosting service"}}
        self.assertEqual(_suggest_term("invoive", rows), "invoice")

    def test_suggestion_none_when_nothing_is_close(self):
        rows = {1: {"text": "The invoice for the hosting service"}}
        self.assertIsNone(_suggest_term("xylophone", rows))


class DidYouMeanTests(unittest.TestCase):
    def test_typo_query_suggests_the_correction(self):
        embedder = HashingEmbedder()
        with tempfile.TemporaryDirectory() as tmp:
            store, embedder = make_store(tmp)
            try:
                add_file(store, "C:\\notes\\bill.txt", "The invoice for the hosting service")
                store.commit()
                parsed = understand("invoive")
                page = run(store, embedder, "invoive", parsed=parsed)
                self.assertIn("bill.txt", [hit.name for hit in page.hits])
                suggestions = [h.value for h in parsed.hints if h.kind == "did_you_mean"]
                self.assertEqual(suggestions, ["invoice"])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
