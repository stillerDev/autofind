"""Backend support for the settings/drawer UI: contracts at the HTTP boundary.

Every test uses a throwaway index home and a real loopback server, so Host,
Origin, Content-Type and the response shapes are exercised together — not just
the handler in isolation.
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import numpy as np

from core import api
from core.config import (
    IndexConfig,
    normalise_patterns,
    portable_status,
    read_index_rules,
    save_index_rules,
)
from core.embed import HashingEmbedder
from core.filevec import to_bits
from core.store import Store


class ConfigRuleTests(unittest.TestCase):
    def test_patterns_validate_and_deduplicate(self):
        self.assertEqual(
            normalise_patterns([" *.tmp ", ".git", "**/node_modules", "*.tmp"]),
            ["*.tmp", ".git", "**/node_modules"],
        )
        # A leading slash is an ignore.py anchor, not an absolute path; drive
        # letters, UNC shares and traversal are the ones to refuse.
        self.assertEqual(normalise_patterns(["/build"]), ["/build"])
        for bad in (["//server/share"], ["C:\\secret"], ["../secret"],
                    ["a\nb"], ["x" * 300], [7]):
            with self.assertRaises(ValueError):
                normalise_patterns(bad)

    def test_rules_round_trip_and_read_is_defensive(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.assertEqual(read_index_rules(home), {"roots": {}, "exclusions": []})
            saved = save_index_rules(home, {
                "roots": {str(home / "docs"): {
                    "enabled": False,
                    "excluded_dirs": ["node_modules"],
                    "exclusions": ["*.bak"],
                    "max_file_mb": 12,
                }},
                "exclusions": ["*.tmp", "!keep.tmp"],
            })
            self.assertEqual(saved["exclusions"], ["*.tmp", "!keep.tmp"])
            self.assertEqual(read_index_rules(home), saved)
            (home / "index-rules.json").write_text("{not json", encoding="utf-8")
            self.assertEqual(read_index_rules(home), {"roots": {}, "exclusions": []})

    def test_portable_status_uses_effective_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "bin" / "AutoFind.exe"
            home = Path(tmp) / "bin" / "data"
            with patch.dict("os.environ", {"AUTOFIND_HOME": str(home)}):
                status = portable_status(home, exe)
            self.assertTrue(status["portable"])
            self.assertEqual(status["data_dir"], str(Path(tmp) / "bin" / "data"))
            with patch.dict("os.environ", {}, clear=True):
                self.assertFalse(portable_status(home, exe)["portable"])

    def test_tail_reads_only_the_end_of_a_large_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "autofind.log"
            path.write_text("".join(f"line {index}\n" for index in range(20000)), encoding="utf-8")
            tail = api._tail_lines(path, 5)
            self.assertEqual(len(tail), 5)
            self.assertEqual(tail[-1], "line 19999")
            big = Path(tmp) / "wide.log"
            big.write_bytes(b"x" * (400 * 1024) + b"last line\n")
            tail = api._tail_lines(big, 1)
            self.assertEqual(len(tail), 1)
            self.assertLessEqual(len(tail[0]), api.TAIL_MAX_LINE_CHARS)
            self.assertTrue(tail[0].endswith("last line"))


class HttpFeatureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        cls.config = IndexConfig(home=base / "home")
        cls.config.ensure_home()
        cls.db = base / "index.db"
        embedder = HashingEmbedder()
        store = Store(cls.db, embedder.name, embedder.dim)
        try:
            store.conn.execute("INSERT INTO dirs(dir_id, path) VALUES(1, 'C:\\corpus')")
            for file_id, name in ((1, "one.txt"), (2, "two.txt")):
                store.conn.execute(
                    "INSERT INTO file_rows(id, dir_id, name, size, mtime, sha256, kind, indexed_at) "
                    "VALUES(?, 1, ?, 10, 1, x'01', 'text', 1)", (file_id, name)
                )
                store.conn.execute(
                    "INSERT INTO file_vectors(file_id, dim, bits) VALUES(?, ?, ?)",
                    (file_id, embedder.dim, to_bits(np.ones(embedder.dim, dtype=np.float32))),
                )
            store.conn.execute(
                "INSERT INTO notes(file_id, text, added_at) VALUES(1, 'remember this', 2.0)"
            )
            store.conn.execute(
                "INSERT INTO aliases(alias, expand) VALUES('cv', 'resume')"
            )
            store.conn.executemany(
                "INSERT INTO recent_searches(q, at) VALUES(?, ?)",
                [("cv", 3.0), ("cv", 4.0), ("report", 5.0)],
            )
            store.conn.execute(
                "INSERT INTO saved(query, label, created, used, last_used) "
                "VALUES('cv', 'CVs', 1.0, 2, 4.0)"
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
            api.MCP = None
        api.close_write_store()
        api.READ_STORES.close_all()
        cls.tmp.cleanup()

    def _request(self, method: str, path: str, payload=None, headers=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request_headers = {"Content-Type": "application/json"}
        request_headers.update(headers or {})
        request = urllib.request.Request(
            self.base + path.lstrip("/"), data=data,
            headers=request_headers, method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as caught:
            return caught.code, json.loads(caught.read().decode("utf-8"))

    def test_vault_status_is_read_only_and_secret_free(self):
        status, body = self._request("GET", "/api/vault")
        self.assertEqual(status, 200)
        self.assertIn("mode", body)
        self.assertIn("present", body)
        self.assertIn("sealed", body)
        self.assertNotIn("passphrase", json.dumps(body).lower().replace("passphrase_env", ""))
        status, _ = self._request("POST", "/api/vault", {"passphrase": "short"})
        self.assertEqual(status, 400)

    def test_autostart_status_reports_enabled_flag(self):
        status, body = self._request("GET", "/api/autostart")
        self.assertEqual(status, 200)
        self.assertIn("enabled", body)
        self.assertIn("supported", body)

    def test_rules_and_exclusions_validate_at_http_boundary(self):
        status, body = self._request("GET", "/api/rules")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"roots": {}, "exclusions": []})
        rules = {
            "roots": {str(Path(self.tmp.name) / "docs"): {"enabled": True, "exclusions": ["*.bak"]}},
            "exclusions": ["*.tmp"],
        }
        status, body = self._request("POST", "/api/rules", rules)
        self.assertEqual(status, 200)
        status, body = self._request("GET", "/api/exclusions")
        self.assertEqual(body["exclusions"], ["*.tmp"])
        status, _ = self._request("POST", "/api/exclusions", {"exclusions": ["C:\\absolute"]})
        self.assertEqual(status, 400)
        status, body = self._request("GET", "/api/rules")
        self.assertEqual(body["exclusions"], ["*.tmp"], "a rejected write must not persist")

    def test_backup_status_and_backup_to_requested_directory(self):
        status, body = self._request("GET", "/api/backup")
        self.assertEqual(status, 200)
        self.assertTrue(body["exists"])
        destination = Path(self.tmp.name) / "backups"
        status, body = self._request("POST", "/api/backup", {"to": str(destination)})
        self.assertEqual(status, 200)
        archive = Path(body["archive"])
        self.assertTrue(archive.is_file())
        self.assertGreater(archive.stat().st_size, 0)
        status, body = self._request("GET", "/api/backup")
        self.assertEqual(body["last"]["archive"], str(archive))
        self.assertTrue(any(item["name"] == archive.name for item in body["archives"]))
        status, _ = self._request("POST", "/api/backup", {"to": "relative/path"})
        self.assertEqual(status, 400)

    def test_portable_and_audit_categories(self):
        status, body = self._request("GET", "/api/portable")
        self.assertEqual(status, 200)
        self.assertIn("portable", body)
        status, body = self._request("GET", "/api/audit")
        self.assertEqual(status, 200)
        self.assertIn("categories", body["sensitive"])

    def test_similar_files_uses_stored_vectors(self):
        status, body = self._request("GET", "/api/similar-files?id=1&limit=5")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["files"]), 1)
        self.assertEqual(body["files"][0]["file_id"], 2)
        self.assertGreater(body["files"][0]["score"], 0)

    def test_notes_crud_and_history_joins_usage(self):
        status, body = self._request("GET", "/api/notes")
        self.assertEqual(status, 200)
        self.assertEqual(body["notes"][0]["text"], "remember this")
        status, body = self._request("GET", "/api/notes?ids=1")
        self.assertEqual(body["notes"], {"1": "remember this"})
        status, body = self._request("GET", "/api/history?limit=10")
        self.assertEqual(status, 200)
        cv = next(item for item in body["history"] if item["query"] == "cv")
        self.assertTrue(cv["saved"])
        self.assertEqual(cv["uses"], 4)  # 2 recent repeats + 2 saved uses
        status, _ = self._request("POST", "/api/note", {"id": 1, "text": "updated note"})
        self.assertEqual(status, 200)
        status, _ = self._request("POST", "/api/note/drop", {"id": 1})
        self.assertEqual(status, 200)
        status, body = self._request("GET", "/api/notes")
        self.assertEqual(body["notes"], [])

    def test_privacy_categories_strict_validation(self):
        status, body = self._request("GET", "/api/privacy/categories")
        self.assertEqual(status, 200)
        self.assertIn("keys", body["available"])
        self.assertTrue(all(item["enabled"] for item in body["categories"]))
        status, _ = self._request("POST", "/api/privacy/categories",
                                  {"categories": ["keys", "not-a-category"]})
        self.assertEqual(status, 400)
        status, body = self._request("POST", "/api/privacy/categories",
                                     {"categories": ["keys"]})
        self.assertEqual(status, 200)
        enabled = [item["id"] for item in body["categories"] if item["enabled"]]
        self.assertEqual(enabled, ["keys"])
        # Restore the default so other tests do not inherit a narrowed guard.
        self._request("POST", "/api/privacy/categories",
                      {"categories": body["available"]})

    def test_translation_indicator_reflects_personal_rewrites(self):
        status, body = self._request("GET", "/api/search?q=cv")
        self.assertEqual(status, 200)
        self.assertTrue(body["translated"])
        self.assertEqual(body["translation"]["by"], "personal")
        self.assertEqual(body["translation"]["original"], "cv")
        self.assertEqual(body["translation"]["rewritten"], "resume")

    def test_reindex_folder_uses_existing_job(self):
        with patch.object(api.JOB, "start", return_value=True) as started:
            status, body = self._request(
                "POST", "/api/reindex-folder", {"folder": self.tmp.name}
            )
        self.assertEqual(status, 202)
        self.assertTrue(body["started"])
        args = started.call_args.args
        self.assertEqual(args[0], [self.tmp.name])
        status, _ = self._request("POST", "/api/reindex-folder",
                                  {"folder": str(Path(self.tmp.name) / "missing")})
        self.assertEqual(status, 400)

    def test_log_and_self_test_viewers_are_bounded_and_live(self):
        status, body = self._request("GET", "/api/log?lines=7")
        self.assertEqual(status, 200)
        self.assertLessEqual(len(body["lines"]), 7)
        status, body = self._request("GET", "/api/self-test")
        self.assertEqual(status, 200)
        self.assertIn("ok", body)

    def test_host_and_content_type_checks_cover_new_routes(self):
        request = urllib.request.Request(
            self.base + "api/rules", headers={"Host": "127.0.0.1:9999"}
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(caught.exception.code, 403)
        request = urllib.request.Request(
            self.base + "api/rules", data=b"{}",
            headers={"Content-Type": "text/plain"}, method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(caught.exception.code, 415)


if __name__ == "__main__":
    unittest.main()
