"""The interface has two languages and must not grow a third of one.

The rule this file enforces is the one that always rots first: a string is added
in English, ships, and the Russian interface keeps saying the old thing. The
dictionary is checked against itself (key parity), against the markup
(`data-i18n` attributes), against the code (`t("…")` calls) and against the
search engine (`chip.*` and `hint.*` kinds the parser can emit).
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from core.query import CHIP_TEMPLATES, HINT_TEMPLATES
from core.query import _TYPE_RULES as TYPE_RULES

WEB = Path(__file__).resolve().parents[1] / "web"
I18N = WEB / "i18n.js"
APP = WEB / "app.js"
PAGE = WEB / "index.html"

# Keys that are built by concatenation, so no literal `t("…")` call mentions
# them: `plural(n, "group.snapshot")` and friends.
PLURAL_BASES = [
    "group.snapshot", "group.copy", "group.version",
    "status.files", "unit.files", "unit.chunks",
]


def _block(source: str, lang: str) -> str:
    match = re.search(rf"\n  {lang}: \{{(.*?)\n  \}},", source, re.S)
    if match is None:
        raise AssertionError(f"no `{lang}` block in i18n.js")
    return match.group(1)


def keys(lang: str) -> set[str]:
    return set(re.findall(r'^\s*"([\w.]+)":', _block(I18N.read_text(encoding="utf-8"), lang), re.M))


class DictionaryTests(unittest.TestCase):
    def test_both_languages_have_the_same_keys(self):
        english, russian = keys("en"), keys("ru")
        self.assertEqual(sorted(english - russian), [], "missing in Russian")
        self.assertEqual(sorted(russian - english), [], "missing in English")

    def test_english_is_the_default(self):
        source = I18N.read_text(encoding="utf-8")
        self.assertIn('const DEFAULT_LANG = "en"', source)

    def test_the_calendar_has_twelve_months_and_seven_days(self):
        # The calendar is ours, so its words are ours too: a comma list that is
        # one word short would silently misname a month.
        source = I18N.read_text(encoding="utf-8")
        for lang in ("en", "ru"):
            block = _block(source, lang)
            months = re.search(r'"cal\.months": "([^"]*)"', block).group(1)
            days = re.search(r'"cal\.weekdays": "([^"]*)"', block).group(1)
            self.assertEqual(len(months.split(",")), 12, f"{lang}: months")
            self.assertEqual(len(days.split(",")), 7, f"{lang}: weekdays")

    def test_plural_bases_have_three_forms(self):
        english, russian = keys("en"), keys("ru")
        for base in PLURAL_BASES:
            for form in ("one", "few", "many"):
                key = f"{base}.{form}"
                self.assertIn(key, english)
                self.assertIn(key, russian)

    def test_no_empty_strings(self):
        source = I18N.read_text(encoding="utf-8")
        self.assertEqual(re.findall(r'":\s*"",', source), [])


class MarkupTests(unittest.TestCase):
    def test_every_data_i18n_attribute_names_a_real_key(self):
        english = keys("en")
        page = PAGE.read_text(encoding="utf-8")
        used = set(re.findall(r'data-i18n(?:-placeholder|-title|-aria)?="([\w.]+)"', page))
        self.assertTrue(used)
        self.assertEqual(sorted(used - english), [])


class CodeTests(unittest.TestCase):
    def test_every_literal_t_call_names_a_real_key(self):
        english = keys("en")
        source = APP.read_text(encoding="utf-8")
        used = set(re.findall(r'\bt\("([\w.]+)"', source))
        self.assertTrue(used)
        self.assertEqual(sorted(used - english), [])

    def test_no_russian_is_left_in_the_script(self):
        # Cyrillic outside of a comment means a string that was never extracted.
        # The boot screen's greeting table is the one legitimate exception: it is
        # data — "Привет" is a word in a language, not a piece of the interface.
        source = APP.read_text(encoding="utf-8")
        # Blank out block comments but keep their newlines, so the line numbers
        # in a failure message still point at the file.
        source = re.sub(
            r"/\*.*?\*/",
            lambda match: re.sub(r"[^\n]", " ", match.group(0)),
            source,
            flags=re.S,
        )
        offenders = []
        in_greetings = False
        for number, line in enumerate(source.splitlines(), start=1):
            if "const BOOT_GREETINGS" in line:
                in_greetings = True
            if in_greetings:
                if line.strip() == "];":
                    in_greetings = False
                continue
            code = line.split("//", 1)[0]
            if re.search(r"[А-Яа-яЁё]", code):
                offenders.append(f"{number}: {line.strip()}")
        self.assertEqual(offenders, [])


class EngineContractTests(unittest.TestCase):
    """The parser speaks kinds; the interface owns the words for them."""

    def test_every_chip_kind_has_a_label(self):
        english, russian = keys("en"), keys("ru")
        for kind in CHIP_TEMPLATES:
            self.assertIn(f"chip.{kind}", english)
            self.assertIn(f"chip.{kind}", russian)

    def test_every_hint_kind_has_a_sentence(self):
        english, russian = keys("en"), keys("ru")
        for kind in HINT_TEMPLATES:
            self.assertIn(f"hint.{kind}", english)
            self.assertIn(f"hint.{kind}", russian)

    def test_weekday_chips_can_be_named(self):
        english = keys("en")
        for day in ("monday", "tuesday", "wednesday", "thursday", "friday",
                    "saturday", "sunday"):
            self.assertIn(f"weekday.{day}", english)

    def test_type_values_are_words_not_kinds(self):
        # "type: image" in the Russian interface was the bug this covers.
        english, russian = keys("en"), keys("ru")
        for _pattern, _kind, value in TYPE_RULES:
            self.assertIn(f"typeVal.{value}", english)
            self.assertIn(f"typeVal.{value}", russian)

    def test_placeholders_match_the_values_the_parser_sends(self):
        # A chip whose template takes no value must not print "{value}" at the
        # user, and one that does must not silently drop the number.
        source = I18N.read_text(encoding="utf-8")
        for lang in ("en", "ru"):
            block = _block(source, lang)
            for kind, template in CHIP_TEMPLATES.items():
                row = re.search(rf'"chip\.{kind}": "([^"]*)"', block)
                self.assertIsNotNone(row, f"{lang}: no chip.{kind}")
                wants = "{value}" in template
                has = "{value}" in row.group(1)
                self.assertEqual(wants, has, f"{lang}: chip.{kind} placeholders")


if __name__ == "__main__":
    unittest.main()
