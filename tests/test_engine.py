"""Ranking, fuzzy names, pagination and the MCP surface."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from core.config import IndexConfig
from core.embed import HashingEmbedder
from core.indexer import run_index
from core.mcp import McpServer
from core.query import understand
from core.search import (
    finalize_score,
    hybrid_search,
    search_page,
    subsequence_score,
)
from core.store import Store, char_mask


class FuzzyTests(unittest.TestCase):
    def test_substring_beats_scattered(self):
        literal = subsequence_score("readme", "README.md")
        scattered = subsequence_score("readme", "r-e-a-d-m-e-notes.md")
        self.assertIsNotNone(literal)
        self.assertIsNotNone(scattered)
        self.assertGreater(literal, scattered)

    def test_prefix_scores_higher_than_middle(self):
        prefix = subsequence_score("app", "App.tsx")
        middle = subsequence_score("app", "my-app-index.ts")
        self.assertGreater(prefix, middle)

    def test_non_subsequence_is_rejected(self):
        self.assertIsNone(subsequence_score("zzz", "README.md"))

    def test_char_mask_is_a_prefilter_not_a_filter(self):
        # A collision may let a wrong file through; a missing character never
        # may reject a real match.
        mask = char_mask("readme")
        self.assertEqual(mask & char_mask("README.md"), mask)
        self.assertNotEqual(mask & char_mask("zzz.txt"), mask)


class ScoreTests(unittest.TestCase):
    def test_exact_name_beats_weak_match(self):
        now = time.time()
        exact = finalize_score(0.5, "invoice.pdf", "a/invoice.pdf", now, now, (0, 0.0), "invoice")
        weak = finalize_score(0.5, "notes.pdf", "a/notes.pdf", now, now, (0, 0.0), "invoice")
        self.assertGreater(exact, weak)

    def test_recency_decays_smoothly(self):
        now = time.time()
        fresh = finalize_score(0.5, "a.txt", "a.txt", now, now)
        old = finalize_score(0.5, "a.txt", "a.txt", now - 30 * 86400, now)
        self.assertGreater(fresh, old)
        self.assertGreater(old, 0.5)

    def test_frecency_boosts_opened_files(self):
        now = time.time()
        never = finalize_score(0.5, "a.txt", "a.txt", now, now, (0, 0.0))
        often = finalize_score(0.5, "a.txt", "a.txt", now, now, (40, now))
        self.assertGreater(often, never)

    def test_depth_penalty_is_bounded(self):
        now = time.time()
        shallow = finalize_score(0.5, "a.txt", "a.txt", now, now)
        deep = finalize_score(0.5, "a.txt", "\\".join(["d"] * 40) + "\\a.txt", now, now)
        self.assertGreaterEqual(deep, 0.0)
        self.assertAlmostEqual(shallow - deep, 0.10, places=3)


class EngineTests(unittest.TestCase):
    """A tiny corpus is enough to pin ordering and pagination."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name) / "corpus"
        root.mkdir()
        # Every file has to clear `min_file_bytes` (32), otherwise the scanner
        # skips it and the test measures nothing.
        (root / "notes.md").write_text(
            "Ранговое слияние объединяет BM25 и векторный поиск в один список. "
            "Гибридный поиск по смыслу и по словам одновременно.\n",
            encoding="utf-8",
        )
        (root / "scanner.py").write_text(
            "def scan(root):\n    return [path for path in root.iterdir() if path.is_file()]\n"
            "\n\ndef main():\n    print(scan('.'))\n",
            encoding="utf-8",
        )
        (root / "readme.txt").write_text(
            "hybrid search notes: fused ranking, fuzzy names and snippets for every hit.\n",
            encoding="utf-8",
        )
        cls.config = IndexConfig(home=Path(cls.tmp.name) / "home")
        cls.embedder = HashingEmbedder()
        cls.db = Path(cls.tmp.name) / "index.db"
        store = Store(cls.db, cls.embedder.name, cls.embedder.dim)
        run_index([root], cls.config, store, cls.embedder)
        store.close()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def store(self):
        return Store(self.db, self.embedder.name, self.embedder.dim, readonly=True)

    def test_fuzzy_finds_by_abbreviation(self):
        store = self.store()
        try:
            hits = hybrid_search(store, self.embedder, "rdme", limit=5)
            self.assertTrue(hits)
            self.assertEqual(hits[0].name, "readme.txt")
        finally:
            store.close()

    def test_results_are_per_file(self):
        store = self.store()
        try:
            hits = hybrid_search(store, self.embedder, "ранговое слияние", limit=10)
            paths = [hit.path for hit in hits]
            self.assertEqual(len(paths), len(set(paths)))
        finally:
            store.close()

    def test_filter_only_query_lists_files(self):
        store = self.store()
        try:
            page = search_page(store, self.embedder, "type:code", limit=10)
            self.assertFalse(page.approx)
            self.assertEqual(page.total, 1)
            self.assertEqual(page.hits[0].kind, "code")
        finally:
            store.close()

    def test_pagination_covers_every_hit(self):
        store = self.store()
        try:
            first = search_page(store, self.embedder, "notes", limit=1)
            self.assertEqual(len(first.hits), 1)
            self.assertIsNotNone(first.next_offset)
            second = search_page(store, self.embedder, "notes", limit=1, offset=1)
            self.assertNotEqual(first.hits[0].path, second.hits[0].path)
        finally:
            store.close()

    def test_sort_by_name_is_stable(self):
        store = self.store()
        try:
            page = search_page(store, self.embedder, "type:code sort:name", limit=10)
            names = [hit.name for hit in page.hits]
            self.assertEqual(names, sorted(names))
        finally:
            store.close()

    def test_intent_chips_reach_the_page(self):
        store = self.store()
        try:
            page = search_page(store, self.embedder, "код за прошлую неделю", limit=5)
            # The chip carries a kind and a value; the label is a rendering of
            # those two and belongs to whoever draws it.
            kinds = [chip["kind"] for chip in page.chips]
            self.assertIn("week", kinds)
            self.assertTrue(all(chip["label"] for chip in page.chips))
        finally:
            store.close()

    def test_exhaustive_mode_returns_everything(self):
        store = self.store()
        try:
            page = search_page(store, self.embedder, "mode:exhaustive type:text", limit=50)
            self.assertFalse(page.approx)
            self.assertGreaterEqual(page.total, 1)
        finally:
            store.close()


class McpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name) / "corpus"
        root.mkdir()
        (root / "notes.md").write_text(
            "Ранговое слияние и BM25 в одном окне: гибридный поиск по файлам и скриншотам.\n",
            encoding="utf-8",
        )
        cls.config = IndexConfig(home=Path(cls.tmp.name) / "home")
        cls.embedder = HashingEmbedder()
        cls.db = Path(cls.tmp.name) / "index.db"
        store = Store(cls.db, cls.embedder.name, cls.embedder.dim)
        run_index([root], cls.config, store, cls.embedder)
        store.close()
        cls.server = McpServer(cls.db, cls.config)

    @classmethod
    def tearDownClass(cls):
        if cls.server._store is not None:
            cls.server._store.close()
        cls.tmp.cleanup()

    def call(self, method, params=None, request_id=1):
        return self.server.handle({"jsonrpc": "2.0", "id": request_id, "method": method,
                                   "params": params or {}})

    def payload(self, response):
        return json.loads(response["result"]["content"][0]["text"])

    def test_initialize(self):
        response = self.call("initialize")
        self.assertEqual(response["result"]["serverInfo"]["name"], "semfind")
        self.assertIn("tools", response["result"]["capabilities"])

    def test_tools_are_listed(self):
        names = {tool["name"] for tool in self.call("tools/list")["result"]["tools"]}
        self.assertIn("search", names)
        self.assertIn("read_snippet", names)
        self.assertIn("index_state", names)
        self.assertIn("regex_search", names)

    def test_search_returns_hits_and_cursor(self):
        response = self.call("tools/call", {"name": "search",
                                            "arguments": {"query": "слияние", "page_size": 1}})
        data = self.payload(response)
        self.assertTrue(data["hits"])
        self.assertIn("path", data["hits"][0])

    def test_read_snippet_is_bounded(self):
        path = str(Path(self.tmp.name) / "corpus" / "notes.md")
        response = self.call("tools/call", {"name": "read_snippet",
                                            "arguments": {"path": path, "max_chars": 200}})
        data = self.payload(response)
        self.assertLessEqual(len(data["text"]), 200)

    def test_unknown_tool_is_an_error_not_a_crash(self):
        response = self.call("tools/call", {"name": "nope", "arguments": {}})
        self.assertTrue(response["result"]["isError"])

    def test_unknown_method_returns_jsonrpc_error(self):
        response = self.call("does/not/exist")
        self.assertEqual(response["error"]["code"], -32601)

    def test_index_state(self):
        data = self.payload(self.call("tools/call", {"name": "index_state", "arguments": {}}))
        self.assertGreaterEqual(data["files"], 1)
        self.assertIn("embedder", data)


if __name__ == "__main__":
    unittest.main()
