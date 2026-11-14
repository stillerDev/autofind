"""The Chinese dictionary is honest: translated, not copied, not truncated.

zh shipped half-English once and nothing failed, because the parity test only
compared en with ru. These tests read the values (not just the keys) and hold
zh to three standards: a value may not still *be* the English string, a value
may not lose a `{placeholder}` the English one has, and a value may not be
plain Latin — the whitelist below is the exception that proves the rule.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "web"
I18N = WEB / "i18n.js"

PAIR = re.compile(r'"([\w.]+)": (?:"((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\')')


def values(lang: str) -> dict[str, str]:
    """Every key->value pair of one locale, unescaped the way JS would read it."""
    source = I18N.read_text(encoding="utf-8")
    match = re.search(rf"\n  {lang}: \{{(.*?)\n  \}},", source, re.S)
    if match is None:
        raise AssertionError(f"no `{lang}` block in i18n.js")
    block = match.group(1)
    out: dict[str, str] = {}
    for hit in PAIR.finditer(block):
        text = hit.group(2) if hit.group(2) is not None else hit.group(3)
        text = text.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")
        out[hit.group(1)] = text
    return out


# Values that are the correct translation *and* the English string: a format
# name, a glob, an arrow diagram, a pair of bare placeholders, SI units the
# Chinese interface spells with Latin letters.
NEUTRAL_IN_ZH = {
    "kind.pdf", "kind.docx", "typeVal.pdf", "cal.monthYear",
    "alias.added", "types.mask", "panel.sealWorking",
    "unit.kb", "unit.mb", "unit.gb",
}

PLACEHOLDER = re.compile(r"\{(\w+)\}")


class ChineseHonestyTests(unittest.TestCase):
    def test_every_key_exists_in_all_three_locales(self):
        en = values("en")
        for lang in ("ru", "zh"):
            other = values(lang)
            self.assertEqual(sorted(set(en) - set(other)), [], f"missing in {lang}")
            self.assertEqual(sorted(set(other) - set(en)), [], f"extra in {lang}")

    def test_every_chinese_value_is_actually_translated(self):
        english, chinese = values("en"), values("zh")
        offenders = [
            key for key, value in chinese.items()
            if key not in NEUTRAL_IN_ZH
            and value == english[key]
            and re.search(r"[A-Za-z]{3,}", value)
        ]
        self.assertEqual(offenders, [], "still English in zh")

    def test_chinese_keeps_every_placeholder(self):
        english, chinese = values("en"), values("zh")
        offenders = []
        for key, en_value in english.items():
            if key not in chinese or key in NEUTRAL_IN_ZH:
                continue
            if sorted(PLACEHOLDER.findall(en_value)) != sorted(PLACEHOLDER.findall(chinese[key])):
                offenders.append(key)
        self.assertEqual(offenders, [], "placeholder drift between en and zh")

    def test_chinese_values_contain_cjk_or_are_whitelisted(self):
        # A "translation" that is plain ASCII Latin is suspicious: every real
        # Chinese sentence carries at least one CJK character.
        chinese = values("zh")
        offenders = [
            key for key, value in chinese.items()
            if key not in NEUTRAL_IN_ZH
            and not re.search(r"[\u4e00-\u9fff]", value)
        ]
        self.assertEqual(offenders, [], "no CJK characters - is this a translation?")

    def test_the_whitelist_still_exists(self):
        # If a whitelisted key is ever translated, the whitelist entry is dead
        # weight and should be removed with the same commit.
        english, chinese = values("en"), values("zh")
        for key in NEUTRAL_IN_ZH:
            self.assertEqual(chinese[key], english[key],
                             f"{key} is now translated - drop it from NEUTRAL_IN_ZH")

    def test_russian_keeps_its_placeholders_too(self):
        # zh got the honesty fence; ru has held the same standard for longer,
        # and one test keeps both on the same contract.
        english, russian = values("en"), values("ru")
        offenders = []
        for key, en_value in english.items():
            if key not in russian:
                continue
            if sorted(PLACEHOLDER.findall(en_value)) != sorted(PLACEHOLDER.findall(russian[key])):
                offenders.append(key)
        self.assertEqual(offenders, [], "placeholder drift between en and ru")


if __name__ == "__main__":
    unittest.main()
