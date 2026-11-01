"""Query grammar and intent compilation."""

from __future__ import annotations

import time
import unittest

from core.query import parse, understand


DAY = 86_400.0


class LexerTests(unittest.TestCase):
    def test_windows_path_keeps_backslashes(self):
        query = parse(r'in:"C:\Users\me\My Docs" отчёт')
        self.assertEqual(query.in_paths, [r"C:\Users\me\My Docs"])
        self.assertEqual([term.text for term in query.terms], ["отчёт"])

    def test_regex_keeps_backslash_class(self):
        query = parse(r're:"\d+"')
        self.assertEqual(query.regexes, [r"\d+"])

    def test_phrase_is_one_term(self):
        query = parse('"exact phrase"')
        self.assertEqual(len(query.terms), 1)
        self.assertTrue(query.terms[0].phrase)
        self.assertEqual(query.terms[0].text, "exact phrase")

    def test_negation_goes_to_exclusions(self):
        query = parse("-node_modules docker")
        self.assertEqual(query.not_paths, ["node_modules"])
        self.assertEqual(query.text, "docker")


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.now = time.time()

    def test_type_resolves_to_kinds(self):
        self.assertEqual(parse("type:image").kinds, {"image"})
        self.assertEqual(parse("type:pdf").kinds, {"pdf"})
        self.assertIn(".xlsx", parse("type:sheet").exts)

    def test_unknown_type_is_read_as_extension(self):
        self.assertEqual(parse("type:md").exts, {".md"})

    def test_modified_within(self):
        query = parse("modified:<7d", self.now)
        self.assertAlmostEqual(query.modified.low, self.now - 7 * DAY, delta=5)
        self.assertIsNone(query.modified.high)
        self.assertTrue(query.modified.matches(self.now - DAY))
        self.assertFalse(query.modified.matches(self.now - 30 * DAY))

    def test_modified_older_than(self):
        query = parse("modified:>30d", self.now)
        self.assertAlmostEqual(query.modified.high, self.now - 30 * DAY, delta=5)
        self.assertTrue(query.modified.matches(self.now - 40 * DAY))

    def test_modified_between(self):
        query = parse("modified:7d..30d", self.now)
        self.assertTrue(query.modified.matches(self.now - 10 * DAY))
        self.assertFalse(query.modified.matches(self.now - DAY))

    def test_size_range(self):
        query = parse("size:>2mb")
        self.assertAlmostEqual(query.size.low, 2 * 1024 * 1024, delta=1)
        self.assertTrue(query.size.matches(3 * 1024 * 1024))
        self.assertFalse(query.size.matches(1024))

    def test_size_accepts_russian_units(self):
        query = parse("size:>2мб")
        self.assertAlmostEqual(query.size.low, 2 * 1024 * 1024, delta=1)

    def test_lang_maps_to_extensions(self):
        self.assertEqual(parse("lang:python").exts, {".py", ".pyi"})

    def test_unknown_key_stays_a_term(self):
        query = parse("weird:value")
        self.assertEqual(query.text, "weird:value")
        self.assertFalse(query.has_filters)

    def test_field_terms_are_split(self):
        query = parse("name:Cargo content:workspace")
        self.assertEqual(query.names, ["Cargo"])
        self.assertEqual(query.contents, ["workspace"])
        self.assertEqual(query.text, "")

    def test_matches_applies_every_filter(self):
        query = parse(r'type:code ext:.py in:"C:\src" -test')
        now = time.time()
        self.assertTrue(query.matches(r"C:\src\app\main.py", "code", 100, now))
        self.assertFalse(query.matches(r"C:\src\app\main.py", "text", 100, now))
        self.assertFalse(query.matches(r"C:\src\test\main.py", "code", 100, now))
        self.assertFalse(query.matches(r"D:\src\main.py", "code", 100, now))
        self.assertFalse(query.matches(r"C:\src\main.js", "code", 100, now))


class IntentTests(unittest.TestCase):
    def setUp(self):
        self.now = time.time()

    def test_screenshots_last_week(self):
        query = understand("скриншоты за прошлую неделю", self.now)
        self.assertEqual(query.kinds, {"image"})
        self.assertIsNotNone(query.modified)
        self.assertAlmostEqual(query.modified.low, self.now - 7 * DAY, delta=60)
        self.assertEqual(query.text, "")
        self.assertEqual(len(query.chips), 2)

    def test_english_screenshots_last_week(self):
        query = understand("screenshots from last week", self.now)
        self.assertEqual(query.kinds, {"image"})
        self.assertIsNotNone(query.modified)

    def test_biggest_files_sorts_by_size(self):
        # Kensaku semantics: «biggest files» is a sort, not a size threshold.
        query = understand("самые большие файлы", self.now)
        self.assertEqual(query.sort, "largest")
        self.assertIsNone(query.size)

    def test_days_ago(self):
        query = understand("3 дня назад ошибка", self.now)
        self.assertAlmostEqual(query.modified.low, self.now - 4 * DAY, delta=60)
        self.assertAlmostEqual(query.modified.high, self.now - 3 * DAY, delta=60)
        self.assertEqual(query.text, "ошибка")

    def test_explicit_filter_disables_intent(self):
        query = understand("type:image скриншоты за неделю", self.now)
        self.assertEqual(query.kinds, {"image"})
        self.assertIn("скриншоты", query.text)
        self.assertEqual(query.chips[0].kind, "type")

    def test_plain_query_is_untouched(self):
        query = understand("смета на ремонт", self.now)
        self.assertEqual(query.text, "смета на ремонт")
        self.assertFalse(query.has_filters)
        self.assertEqual(query.chips, [])

    def test_chip_keeps_source_words(self):
        query = understand("отчёт за прошлый месяц", self.now)
        sources = [chip.source for chip in query.chips]
        self.assertTrue(any("месяц" in source for source in sources))
        self.assertEqual(query.text, "отчёт")


if __name__ == "__main__":
    unittest.main()
