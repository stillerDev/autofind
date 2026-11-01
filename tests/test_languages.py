"""The language table, the coverage report, and the boot screen's greeting.

The greeting is not decoration: it is drawn from the languages the OCR on this
machine can actually read, so these tests are really about the claim "we read
these languages" staying true to what the engines report.
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from core.config import IndexConfig
from core.ocr import coverage, engine_report, supported_languages
from core.ocr import languages as table


class LanguageTableTests(unittest.TestCase):
    def test_every_entry_has_a_greeting_and_a_name(self):
        for language in table.LANGUAGES:
            with self.subTest(tag=language.tag):
                self.assertTrue(language.hello.strip())
                self.assertTrue(language.name.strip())
                self.assertNotEqual(language.hello, language.name)

    def test_tags_are_unique(self):
        tags = [language.tag for language in table.LANGUAGES]
        self.assertEqual(len(tags), len(set(tags)))

    def test_model_names_are_known(self):
        for language in table.LANGUAGES:
            if language.model is None:
                continue
            with self.subTest(tag=language.tag):
                self.assertIn(language.model, table.MODELS)

    def test_windows_tags_fall_back_to_the_base_language(self):
        self.assertEqual(table.for_tag("ru-RU").tag, "ru")
        self.assertEqual(table.for_tag("en-GB").tag, "en")
        # zh-Hans-CN must not become "zh-hans"; the table keys on the script
        # subtag only where it exists.
        self.assertEqual(table.for_tag("zh-Hans-CN").tag, "zh")
        self.assertEqual(table.for_tag("zh-TW").tag, "zh-Hant" if "zh-Hant" in table.BY_TAG else "zh")
        self.assertIsNone(table.for_tag("xx-YY"))
        self.assertIsNone(table.for_tag(""))
        self.assertIsNone(table.for_tag(None))

    def test_every_model_has_at_least_one_language(self):
        # A model with no language in the table would be downloaded and never
        # used; a language with an unknown model would never be routed.
        for model in table.MODELS:
            with self.subTest(model=model):
                self.assertTrue(table.with_model(model))

    def test_pool_is_never_empty(self):
        self.assertEqual([language.tag for language in table.greeting_pool(set())], ["en"])


class CoverageTests(unittest.TestCase):
    def test_coverage_counts_what_it_lists(self):
        cover = coverage()
        self.assertEqual(cover["total"], len(cover["languages"]))
        for language in cover["languages"]:
            self.assertIn("hello", language)
            self.assertIn(language["model"], cover["models"])

    def test_report_agrees_with_coverage(self):
        report = engine_report("rus+eng")
        self.assertEqual(report["supported_languages"], report["coverage"]["total"])
        self.assertEqual(report["supported_languages"], len(supported_languages()))

    def test_report_is_cached(self):
        self.assertIs(engine_report("rus+eng"), engine_report("rus+eng"))


class ReadyEndpointTests(unittest.TestCase):
    """`/api/ready` is what the boot screen polls; it must answer before the
    index is warm, and then flip."""

    def setUp(self):
        from core.api import start_server

        self.tmp = tempfile.TemporaryDirectory()
        self.config = IndexConfig(home=Path(self.tmp.name) / "home")
        self.config.ensure_home()
        self.server, self.url = start_server(
            self.config.db_path, self.config, port=8791
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def _get(self, path: str) -> dict:
        with urllib.request.urlopen(self.url.rstrip("/") + path, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_ready_has_a_stage_and_an_uptime(self):
        state = self._get("/api/ready")
        self.assertIn("ready", state)
        self.assertIn(state["stage"], {"boot", "vectors", "ready"})
        self.assertGreaterEqual(state["uptime_ms"], 0)

    def test_ready_flips_after_the_warmup(self):
        from core.api import READY

        READY["ready"] = True
        self.assertTrue(self._get("/api/ready")["ready"])
        READY["ready"] = False

    def test_ocr_reports_languages_for_the_boot_screen(self):
        data = self._get("/api/ocr")
        cover = data["coverage"]
        self.assertTrue(cover["languages"])
        first = cover["languages"][0]
        self.assertTrue({"tag", "name", "hello"} <= set(first))
        self.assertEqual(cover["total"], len(cover["languages"]))


if __name__ == "__main__":
    unittest.main()
