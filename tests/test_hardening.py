"""Regression tests for the hardening fixes.

Each test pins one failure that was real: a blocked regex query, a CSV formula
injection, a tied-timestamp trim, a bracketed IPv6 Host header, and the
explicit fuzzy-search backend selection.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core import api, store as store_mod
from core.nameindex import NameIndex
from core.search import finalize_score, fuzzy_files
from core.store import Store


class RegexGuardTests(unittest.TestCase):
    def test_nested_quantifier_is_refused(self):
        self.assertTrue(store_mod._regex_is_unsafe(r"(a+)+$"))
        self.assertTrue(store_mod._regex_is_unsafe(r"(?:ab*)*"))
        self.assertTrue(store_mod._regex_is_unsafe("a{5000}"))
        self.assertTrue(store_mod._regex_is_unsafe("x" * store_mod.MAX_REGEX_PATTERN + "1"))

    def test_ordinary_pattern_is_accepted(self):
        self.assertFalse(store_mod._regex_is_unsafe(r"TODO|FIXME"))
        self.assertFalse(store_mod._regex_is_unsafe(r"\b\w{2,8}\b"))


class CsvCellTests(unittest.TestCase):
    def test_formula_markers_are_neutralised(self):
        for value in ("=1+1", "+cmd", "-sum", "@import", "\tcalc"):
            self.assertTrue(api._csv_cell(value).startswith("'"))
        self.assertEqual(api._csv_cell("отчёт.pdf"), "отчёт.pdf")


class HighlightEscapeTests(unittest.TestCase):
    def test_indexed_markup_cannot_become_html(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        root = Path(__file__).resolve().parent.parent
        result = subprocess.run(
            [node, str(root / "tests" / "test_xss.js")],
            cwd=root, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class HostHeaderTests(unittest.TestCase):
    def _ok(self, host: str, origin: str | None = None) -> bool:
        headers = {"Host": host}
        if origin:
            headers["Origin"] = origin
        return api.Handler._host_ok(SimpleNamespace(headers=headers))

    def test_ipv6_loopback_is_accepted(self):
        self.assertTrue(self._ok("[::1]:8765"))
        self.assertTrue(self._ok("127.0.0.1:8765"))

    def test_foreign_host_and_origin_are_rejected(self):
        self.assertFalse(self._ok("evil.example"))
        self.assertFalse(self._ok("127.0.0.1:8765", "https://evil.example"))


class RecentSearchTrimTests(unittest.TestCase):
    def test_tied_timestamps_still_keep_exactly_ten(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "index.db", "hashing-384", 384)
            try:
                with patch("core.store.time.time", return_value=1_700_000_000.0):
                    for index in range(12):
                        store.save_search_query(f"query {index}")
                store.commit()
                count = store.conn.execute("SELECT COUNT(*) FROM recent_searches").fetchone()[0]
                self.assertEqual(count, 10)
            finally:
                store.close()  # Windows must release index.db before the tmp dir goes


class FuzzyBackendTests(unittest.TestCase):
    def test_explicit_flag_overrides_import_order(self):
        calls: list[str] = []

        class FakeStore:
            pass

        # Patch the module's `sys` reference, not sys.modules itself: replacing
        # the real dict breaks imports inside unrelated code.
        fake_sys = SimpleNamespace(modules={"numpy": object()})
        with patch.dict(fuzzy_files.__globals__, {"sys": fake_sys}), \
             patch("core.search.nameindex.load") as load, \
             patch("core.search.nameindex.search_rows") as rows:
            load.return_value.search.side_effect = lambda text, limit: calls.append("numpy") or []
            rows.side_effect = lambda store, text, limit: calls.append("sql") or []
            fuzzy_files(FakeStore(), "x", 5, fast=False)
            fuzzy_files(FakeStore(), "x", 5, fast=True)
        self.assertEqual(calls, ["sql", "numpy"])


class PinUiTests(unittest.TestCase):
    def test_unpin_repaints_idle_list(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        root = Path(__file__).resolve().parent.parent
        result = subprocess.run(
            [node, str(root / "tests" / "test_pin_ui.js")],
            cwd=root, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class SearchCacheEvictionTests(unittest.TestCase):
    def test_bytes_values_evict_without_unpacking(self):
        cache = {"a": b"x" * 30, "b": b"y" * 20}
        with patch.object(api, "SEARCH_CACHE_LIMIT", 2), \
                patch.object(api, "SEARCH_CACHE_MAX_BYTES", 25):
            api.prune_search_cache(cache)
        self.assertEqual(cache, {"b": b"y" * 20})


class MultiWordNameTests(unittest.TestCase):
    """`browser extension` must find BROWSER_EXTENSION.md, not only `name:`."""

    @staticmethod
    def _row(file_id: int, name: str, folder: str = "chess-ai") -> dict:
        return {
            "id": file_id,
            "path": f"C:\\corpus\\{folder}\\{name}",
            "name": name,
            "char_mask": 0,
            "kind": "text",
            "size": 100,
            "mtime": 1_700_000_000.0 + file_id,
        }

    def test_name_index_matches_all_tokens_across_separators(self):
        rows = [
            self._row(1, "BROWSER_EXTENSION.md"),
            self._row(2, "browser-extension.txt"),
            self._row(3, "README.md"),
        ]
        index = NameIndex.from_rows(rows)
        found = [file_id for file_id, _ in index.search("browser extension", 5)]
        self.assertEqual(found[:2], [1, 2])

    def test_folder_token_counts_as_a_match(self):
        rows = [self._row(1, "summary.md", "chess-ai"), self._row(2, "summary.md", "other")]
        index = NameIndex.from_rows(rows)
        found = [file_id for file_id, _ in index.search("chess summary", 5)]
        self.assertEqual(found[0], 1)

    def test_finalize_score_boosts_all_token_matches(self):
        common = dict(base=0.1, mtime=1_700_000_000.0, now=1_700_000_000.0)
        exact = finalize_score(
            name="BROWSER_EXTENSION.md", path="C:\\corpus\\BROWSER_EXTENSION.md",
            text="browser extension", **common,
        )
        folder = finalize_score(
            name="summary.md", path="C:\\corpus\\chess-ai\\summary.md",
            text="chess summary", **common,
        )
        unrelated = finalize_score(
            name="notes.txt", path="C:\\corpus\\notes.txt",
            text="browser extension", **common,
        )
        self.assertGreater(exact, folder)
        self.assertGreater(folder, unrelated)


if __name__ == "__main__":
    unittest.main()
