"""The OpenAPI route: served live, describes the surface that actually exists."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from core import api
from core.config import IndexConfig
from core.embed import HashingEmbedder
from core.indexer import run_index
from core.openapi import ROUTES
from core.store import Store


class OpenApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name) / "corpus"
        root.mkdir()
        (root / "notes.txt").write_text("openapi test\n", encoding="utf-8")
        cls.config = IndexConfig(home=Path(cls.tmp.name) / "home")
        cls.config.ensure_home()
        cls.db = Path(cls.tmp.name) / "index.db"
        embedder = HashingEmbedder()
        store = Store(cls.db, embedder.name, embedder.dim)
        run_index([root], cls.config, store, embedder)
        store.close()
        cls.server, cls.base = api.start_server(cls.db, cls.config, port=8792)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.tmp.cleanup()

    def test_served_document_is_valid_openapi(self):
        with urllib.request.urlopen(f"{self.base}/api/openapi.json") as resp:
            doc = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(doc["openapi"], "3.0.3")
        self.assertTrue(doc["info"]["version"])
        # every documented path has at least one operation
        for path, item in doc["paths"].items():
            self.assertTrue(item, f"{path} has no operations")
        # and the document knows the port it was served on
        self.assertIn(str(self.server.server_address[1]), doc["servers"][0]["url"])

    def test_documented_routes_match_the_dispatch(self):
        """Every documented path is one the router really handles."""
        with urllib.request.urlopen(f"{self.base}/api/openapi.json") as resp:
            doc = json.loads(resp.read().decode("utf-8"))
        documented = {(op, path) for path, item in doc["paths"].items() for op in item}
        known = {(r[0], r[1]) for r in ROUTES}
        self.assertEqual(
            documented, known,
            "ROUTES and the served document drifted apart",
        )


if __name__ == "__main__":
    unittest.main()
