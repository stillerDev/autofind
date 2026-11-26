"""Regressions for the API/OpenAPI findings in AUDIT_REPORT.md §2.1 and §3.

These pin the contract at the HTTP boundary and on the shared helpers: origin
and port checks, bounded read-store pool, locked cache writes, real bound port
for prewarm, honest audit counts, and a complete OpenAPI route table.
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core import api
from core.__version__ import __version__
from core.config import IndexConfig
from core.openapi import ROUTES, _RESPONSE_CODES, document
from core.store import Store


class VersionAndHardcodeTests(unittest.TestCase):
    def test_server_version_uses_single_source(self):
        self.assertEqual(api.Handler.server_version, f"AutoFind/{__version__}")

    def test_no_developer_path_or_stale_date_in_api(self):
        source = Path(api.__file__).read_text(encoding="utf-8")
        self.assertNotIn("h1ile", source)
        self.assertNotIn("2026-10-25", source)
        self.assertLessEqual(api.PREWARM_RECENT_QUERIES, 5)


class DefaultRootsTests(unittest.TestCase):
    def test_localized_known_folders_are_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            localized = {}
            for key in api._KNOWN_FOLDERS:
                localized[key] = home / key
                localized[key].mkdir()
            lookup = {guid: str(localized[key]) for key, guid in api._KNOWN_FOLDERS.items()}
            with patch.object(api, "_known_folder_path", side_effect=lambda guid: lookup[guid]), \
                 patch.object(api.Path, "home", return_value=home):
                roots = api._default_roots()
            self.assertIn(str(localized["Documents"]), roots)
            self.assertIn(str(localized["Downloads"]), roots)


class HostAndContentTypeTests(unittest.TestCase):
    @staticmethod
    def _handler(host: str, origin: str | None = None, port: int = 8765):
        headers = {"Host": host, "Content-Type": "application/json"}
        if origin:
            headers["Origin"] = origin
        return SimpleNamespace(
            headers=headers,
            server=SimpleNamespace(server_address=("127.0.0.1", port)),
        )

    def test_wrong_port_on_host_is_rejected(self):
        self.assertFalse(api.Handler._host_ok(self._handler("127.0.0.1:9999")))

    def test_origin_must_match_host_scheme_and_port(self):
        self.assertTrue(api.Handler._host_ok(
            self._handler("127.0.0.1:8765", "http://127.0.0.1:8765")))
        self.assertFalse(api.Handler._host_ok(
            self._handler("127.0.0.1:8765", "http://localhost:8765")))
        self.assertFalse(api.Handler._host_ok(
            self._handler("127.0.0.1:8765", "http://127.0.0.1:8080")))
        self.assertFalse(api.Handler._host_ok(
            self._handler("127.0.0.1:8765", "https://127.0.0.1:8765")))


class IntParamTests(unittest.TestCase):
    def test_bad_and_negative_values_are_clamped(self):
        self.assertEqual(api._int_param({"limit": ["-1"]}, "limit", 40, 1, 200), 1)
        self.assertEqual(api._int_param({"limit": ["9999"]}, "limit", 40, 1, 200), 200)
        self.assertEqual(api._int_param({"limit": ["not-an-int"]}, "limit", 40, 1, 200), 40)
        self.assertEqual(api._int_param({}, "limit", 40, 1, 200), 40)


class SearchCacheLockTests(unittest.TestCase):
    def test_clear_waits_for_the_lock_held_by_the_iterator(self):
        original = api.SEARCH_CACHE
        api.SEARCH_CACHE = {"a": b"1", "b": b"2"}
        try:
            done = threading.Event()
            with api.SEARCH_LOCK:
                thread = threading.Thread(target=lambda: (api.clear_search_cache(), done.set()), daemon=True)
                thread.start()
                self.assertFalse(done.wait(0.1), "clear() must not race prune()")
            self.assertTrue(done.wait(2.0))
            self.assertEqual(api.SEARCH_CACHE, {})
        finally:
            api.SEARCH_CACHE = original


class ReadStoresPoolTests(unittest.TestCase):
    def test_loan_count_is_bounded(self):
        pool = api._ReadStores(limit=2)
        created: list[int] = []
        counter = [0]

        def factory():
            counter[0] += 1
            created.append(counter[0])
            return SimpleNamespace(close=lambda: None)

        first = pool.acquire(factory)
        second = pool.acquire(factory)
        done = threading.Event()
        third = []

        def late():
            third.append(pool.acquire(factory))
            done.set()

        thread = threading.Thread(target=late, daemon=True)
        thread.start()
        self.assertFalse(done.wait(0.1), "a third Store must wait for a free loan")
        pool.release(first)
        self.assertTrue(done.wait(2.0))
        self.assertEqual(counter[0], 2, "no third Store may be opened while two are lent out")
        pool.release(second)
        pool.release(third[0])


class SealStatusTests(unittest.TestCase):
    def test_reads_one_locked_copy(self):
        captured = {}
        handler = SimpleNamespace(
            config=SimpleNamespace(home=Path("unused")),
            db_path=Path("unused.db"),
            _json=lambda payload, status=200: captured.update(payload),
        )
        with api._SEAL_LOCK:
            original = dict(api._SEAL)
            api._SEAL.update({"working": True, "action": "on", "done": 3,
                              "total": 9, "stage": "rewrite", "error": None})
        try:
            api.Handler._seal_status(handler)
            self.assertEqual(captured["done"], 3)
            self.assertEqual(captured["stage"], "rewrite")
        finally:
            with api._SEAL_LOCK:
                api._SEAL.clear()
                api._SEAL.update(original)


class OpenApiContractTests(unittest.TestCase):
    def test_all_implemented_routes_are_documented(self):
        known = {(method, path) for method, path, *_ in ROUTES}
        for method, path in (
            ("get", "/api/self-test"), ("get", "/api/health"),
            ("get", "/api/notes"), ("get", "/api/tag-colors"),
            ("get", "/api/duplicates/stream"), ("post", "/api/index"),
            ("post", "/api/reindex-one"), ("post", "/api/note"),
            ("post", "/api/tag-color"),
        ):
            self.assertIn((method, path), known)

    def test_parameters_match_the_handlers(self):
        routes = {(method, path): (params, body) for method, path, _, params, body in ROUTES}
        self.assertEqual(routes[("get", "/api/open")][0], ["id"])
        self.assertEqual(routes[("get", "/api/reveal")][0], ["id"])
        self.assertEqual(routes[("post", "/api/reindex")][1], [])
        self.assertEqual(routes[("post", "/api/collections")][1],
                         ["action", "id", "ids", "name"])

    def test_responses_come_from_the_code_table(self):
        doc = document(9876)
        get = doc["paths"]["/api/search"]["get"]["responses"]
        post = doc["paths"]["/api/index"]["post"]["responses"]
        self.assertEqual(set(get), set(_RESPONSE_CODES["get"]))
        self.assertEqual(set(post), set(_RESPONSE_CODES["post"]))
        self.assertIn("202", post)
        self.assertIn("415", post)
        self.assertIn("403", get)
        self.assertIn("servers", doc)
        self.assertEqual(doc["servers"][0]["url"], "http://127.0.0.1:9876")


class HttpContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.config = IndexConfig(home=Path(cls.tmp.name) / "home")
        cls.config.ensure_home()
        cls.db = Path(cls.tmp.name) / "index.db"
        store = Store(cls.db, "hashing-384", 384)
        try:
            # More than the old all_tags(limit=1000) cap: the audit report must
            # count every tag, not just the first page.
            store.conn.execute("INSERT INTO dirs(dir_id, path) VALUES(1, 'C:\\corpus')")
            store.conn.execute(
                "INSERT INTO file_rows(id, dir_id, name, size, mtime, sha256, kind, indexed_at) "
                "VALUES(1, 1, 'one.txt', 0, 0, x'', 'text', 0)"
            )
            store.conn.executemany(
                "INSERT INTO tags(file_id, tag, added_at) VALUES(1, ?, 0)",
                [(f"tag{index:04d}",) for index in range(1005)],
            )
            store.commit()
        finally:
            store.close()
        cls.server, cls.base = api.start_server(cls.db, cls.config, port=0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        if api.MCP is not None:
            api.MCP.close()
        api.READ_STORES.close_all()
        cls.tmp.cleanup()

    def _get(self, path: str):
        with urllib.request.urlopen(self.base + path.lstrip("/"), timeout=10) as response:
            return response.status, response.read(), response.headers

    def test_start_server_reports_the_real_bound_port(self):
        self.assertEqual(int(self.base.rstrip("/").rsplit(":", 1)[1]),
                         self.server.server_address[1])
        self.assertNotEqual(self.server.server_address[1], 0)

    def test_get_mcp_has_allow_header(self):
        request = urllib.request.Request(self.base + "mcp", method="GET")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(caught.exception.code, 405)
        self.assertEqual(caught.exception.headers["Allow"], "POST")

    def test_ambient_post_requires_application_json(self):
        request = urllib.request.Request(
            self.base + "api/focus", data=b"{}",
            headers={"Content-Type": "text/plain"}, method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(caught.exception.code, 415)

    def test_audit_counts_all_tags(self):
        status, body, _ = self._get("api/audit")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["personal"]["tags"], 1005)


if __name__ == "__main__":
    unittest.main()
