"""MCP cursor pagination: stable pages, honest errors, no silent restarts."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.config import IndexConfig
from core.embed import HashingEmbedder
from core.indexer import run_index
from core.mcp import McpServer
from core.store import Store


def json_payload(response: dict) -> dict:
    """The parsed tool payload of a successful tools/call."""
    return json.loads(response["result"]["content"][0]["text"])

def text_of(response: dict) -> str:
    """The raw text of a tool response (used for error payloads)."""
    return response["result"]["content"][0]["text"]


class CursorPaginationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name) / "corpus"
        root.mkdir()
        for index in range(7):
            (root / f"mod{index}.py").write_text(
                f"def parse_query_{index}():\n    return {index}\n",
                encoding="utf-8",
            )
        cls.db = Path(cls.tmp.name) / "index.db"
        embedder = HashingEmbedder()
        store = Store(cls.db, embedder.name, embedder.dim)
        run_index([root], IndexConfig(home=Path(cls.tmp.name) / "home"), store, embedder)
        store.close()
        cls.server = McpServer(cls.db, IndexConfig(home=Path(cls.tmp.name)))

    @classmethod
    def tearDownClass(cls):
        if cls.server._store is not None:
            cls.server._store.close()
        cls.tmp.cleanup()

    def test_find_symbol_pages_do_not_overlap(self):
        first = json_payload(self.server.handle({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "find_symbol",
                       "arguments": {"name": "parse_query", "page_size": 3}},
        }))
        self.assertEqual(len(first["definitions"]), 3)
        self.assertIsNotNone(first["next_cursor"])
        second = json_payload(self.server.handle({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "find_symbol",
                       "arguments": {"name": "parse_query", "page_size": 3,
                                     "cursor": first["next_cursor"]}},
        }))
        first_paths = [d["path"] for d in first["definitions"]]
        second_paths = [d["path"] for d in second["definitions"]]
        self.assertFalse(set(first_paths) & set(second_paths))
        self.assertEqual(len(second_paths), 3)
        # 7 symbols total: page three holds the last one and no next cursor
        third = json_payload(self.server.handle({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "find_symbol",
                       "arguments": {"name": "parse_query", "page_size": 3,
                                     "cursor": second["next_cursor"]}},
        }))
        self.assertEqual(len(third["definitions"]), 1)
        self.assertIsNone(third["next_cursor"])

    def test_bad_cursor_is_an_error_not_a_restart(self):
        response = self.server.handle({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "search",
                       "arguments": {"query": "parse_query", "cursor": "junk"}},
        })
        message = text_of(response)
        # the error names the cursor, so the agent can re-read page one
        self.assertIn("cursor", message)
        self.assertIn("junk", message)

    def test_page_size_bounds_are_enforced(self):
        payload = json_payload(self.server.handle({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "search",
                       "arguments": {"query": "parse_query", "page_size": 0}},
        }))
        hits = payload["hits"]
        self.assertTrue(hits)  # 0 fell back to the default page, not an empty one


if __name__ == "__main__":
    unittest.main()
